"""Pico PIR as an App Base product: a PIR motion sensor on a Pico W, reported over MQTT and on its page.

The base does the web page and API, the settings, MQTT and Home Assistant, Wi-Fi, setup mode, updates and the clock
(base/pico). This is what's Pico PIR's own: the sensor's settings, its state, the commands on the set topic and its
Home Assistant entities, all as in the firmware before the base, so Home Assistant carries on as it was.
"""
from base.settings import Setting, is_bool, is_int
from version import VERSION  # noqa: F401  (scripts/wifi_update.py reads it there)

NAME = 'Pico PIR'
# The setup hotspot and the start of a name made from the board's id
SETUP_SSID = 'PicoPIR-SETUP'
NAME_PREFIX = 'PicoPIR-'
MANUFACTURER = 'Raspberry Pi'
MODEL = 'Pico W'

CLEAR_AFTER_MIN_S = 5
CLEAR_AFTER_MAX_S = 3600

SETTINGS = {
    'device': {
        # The GPIO the PIR sensor's output is wired to
        'pir_pin': Setting(22, is_int(0, 28), restart=True),
    },
    'sensor': {
        # Read the sensor at all; off, motion is never reported
        'enabled': Setting(True, is_bool),
        # Seconds without motion before it's reported clear (see motion.py)
        'clear_after_s': Setting(15, is_int(CLEAR_AFTER_MIN_S, CLEAR_AFTER_MAX_S)),
    },
}
# The hardware, not a preference: a reset keeps it
RESET_KEEPS = (('device', 'pir_pin'),)

# Home Assistant: the motion, the last motion's time, and the two sensor settings
HA_ENTITIES = (('binary_sensor', 'motion'), ('sensor', 'last_motion'), ('number', 'clear_after_s'),
               ('switch', 'sensing'))

_sensor = None


def tasks(device):
    global _sensor
    from base import pico
    from motion import MotionSensor
    _sensor = MotionSensor(device)
    pico.on_clock_moved(_sensor.shift_clock)
    return [_sensor.run()]


def _iso(t):
    """A time.time() as local ISO 8601 with its offset, or None (until the clock is set it says 2021)."""
    from base import pico
    from base.pico import clock
    now, offset = pico.local_time()
    return clock.iso(t, offset) if t is not None and now is not None else None


def info(device):
    """The sensor for /api/info: now, the last motion, the count since the start and the latest detections."""
    if _sensor is None:
        return {}
    motion = _sensor.info()
    motion['last'] = _iso(motion['last'])
    motion['recent'] = [[_iso(t) for t in detection] for detection in motion['recent']]
    return {'motion': motion}


def state(device):
    """The state topic: what the sensor sees, and its settings. last is when the latest movement started, last_end
    when the sensor last saw it (null while it's moving), without the clear time."""
    motion = _sensor.info()
    latest = motion['recent'][0] if motion['recent'] else None
    return {'motion': motion['active'], 'last': _iso(motion['last']),
            'last_end': _iso(latest[1]) if latest else None, 'count': motion['count'],
            'enabled': motion['enabled'], 'clear_after_s': device.settings.get('sensor', 'clear_after_s')}


def set(device, command):
    """A command on the set topic: {"enabled": true | false} and/or {"clear_after_s": 5-3600}."""
    if any(key not in ('enabled', 'clear_after_s') for key in command):
        return 'only enabled and clear_after_s can be set'
    value = command.get('clear_after_s')
    if isinstance(value, float) and value == int(value):  # 20.0, from a slider
        command['clear_after_s'] = int(value)
    error, changed = device.settings.update({'sensor': command})
    if not error:
        device.applied(changed)
    return error


def applied(device, changed):
    # The sensor reads its settings on every poll; the state topic shows them
    if any(section == 'sensor' for section, _ in changed):
        device.state_changed()


def ha_entity(device, key, t):
    """Each entity's discovery config. No room is given: Home Assistant asks for one."""
    if key == 'motion':
        return {'name': 'Motion', 'device_class': 'motion',
                'value_template': "{{ 'ON' if value_json.motion else 'OFF' }}"}
    if key == 'last_motion':
        # null until the clock is set, which Home Assistant shows as unknown
        return {'name': 'Last motion', 'device_class': 'timestamp', 'value_template': '{{ value_json.last }}'}
    if key == 'clear_after_s':
        return {'name': 'Clear after', 'device_class': 'duration', 'unit_of_measurement': 's',
                'min': CLEAR_AFTER_MIN_S, 'max': CLEAR_AFTER_MAX_S, 'step': 1, 'mode': 'box',
                'entity_category': 'config', 'value_template': '{{ value_json.clear_after_s }}',
                'command_topic': t['set'], 'command_template': '{"clear_after_s": {{ value | int }}}'}
    return {'name': 'Sensing', 'icon': 'mdi:motion-sensor', 'entity_category': 'config',
            'value_template': "{{ 'ON' if value_json.enabled else 'OFF' }}", 'state_on': 'ON', 'state_off': 'OFF',
            'command_topic': t['set'], 'payload_on': '{"enabled": true}', 'payload_off': '{"enabled": false}'}
