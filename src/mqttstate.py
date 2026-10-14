"""MQTT topics and payloads for reporting state, plus Home Assistant discovery.

Pure functions, so they can be tested off the device. app.py does the
publishing and subscribing.

The topics hang off a configurable base (mqtt.topic_base, where {name} is the
device name) and Home Assistant discovery prefix (mqtt.ha_prefix):

  <base>/status       retained: "online", or "offline" (the will)
  <base>/info         retained: {"version", "uptime_s", "rssi", "ip", "mem_free"}, every minute
  <base>/state        retained: {"motion", "last", "count", "enabled", "clear_after_s"}, on every change
  <base>/set          commands: {"enabled": true/false} and/or {"clear_after_s": 5-3600}
  <ha_prefix>/sensor/<name>/<key>/config   retained discovery config for the info sensors (if enabled)
  <ha_prefix>/<component>/<name>/<key>/config   the same for the motion entities (HA_ENTITIES)
"""
import ujson

ONLINE = 'online'
OFFLINE = 'offline'

DEFAULT_TOPIC_BASE = '/home/device/{name}'
DEFAULT_HA_PREFIX = 'homeassistant'
MAX_TOPIC_PREFIX_LEN = 100

INFO_INTERVAL_S = 60

# The info figures Home Assistant shows as sensors (mqttha.py describes them).
SENSOR_KEYS = ('rssi', 'uptime_s', 'mem_free', 'ip')

# The motion entities Home Assistant shows, and their kind (mqttha.py describes them).
HA_ENTITIES = (('motion', 'binary_sensor'), ('last_motion', 'sensor'), ('clear_after_s', 'number'),
               ('sensing', 'switch'))

# The topics the device publishes retained, so ones it stops using need clearing.
RETAINED = (('status', 'info', 'state') + tuple('ha_sensor_' + k for k in SENSOR_KEYS) +
            tuple('ha_' + k for k, _ in HA_ENTITIES))

# What the set topic can change: the sensor settings of the same names.
SETTABLE = ('enabled', 'clear_after_s')


def _prefix_error(value, setting, placeholder):
    if not isinstance(value, str) or not value:
        return setting + ' cannot be empty'
    if len(value) > MAX_TOPIC_PREFIX_LEN:
        return setting + ' must be at most ' + str(MAX_TOPIC_PREFIX_LEN) + ' characters'
    if '+' in value or '#' in value:
        return setting + ' cannot contain the wildcards + or #'
    if '\0' in value:
        return setting + ' cannot contain a null character'
    if value != value.strip():
        return setting + ' cannot start or end with a space'
    if value.endswith('/'):
        return setting + ' cannot end with /'
    if '//' in value:
        return setting + ' cannot contain an empty level (//)'
    rest = value.replace('{name}', '') if placeholder else value
    if '{' in rest or '}' in rest:
        return setting + (' only understands the placeholder {name}' if placeholder else ' cannot contain { or }')
    return None


def topic_base_error(base):
    """None if base works as mqtt.topic_base, else why not (starting with the setting's name)."""
    return _prefix_error(base, 'topic_base', True)


def ha_prefix_error(prefix):
    """None if prefix works as mqtt.ha_prefix, else why not."""
    return _prefix_error(prefix, 'ha_prefix', False)


def topics(client_id, base=DEFAULT_TOPIC_BASE, ha_prefix=DEFAULT_HA_PREFIX):
    """The device's topics."""
    base = base.replace('{name}', client_id)
    t = {
        'status': base + '/status',
        'info': base + '/info',
        'state': base + '/state',
        'set': base + '/set',
    }
    for key in SENSOR_KEYS:
        t['ha_sensor_' + key] = ha_prefix + '/sensor/' + client_id + '/' + key + '/config'
    for key, component in HA_ENTITIES:
        t['ha_' + key] = ha_prefix + '/' + component + '/' + client_id + '/' + key + '/config'
    return t


def retired_topics(old, new):
    """The retained topics in old (a topics() dict) that new no longer uses."""
    current = new.values()
    return [old[k] for k in RETAINED if old[k] not in current]


def info_payload(info):
    """info: DeviceAPI.get_device_info(). The health figures, as /api/info has them."""
    return ujson.dumps({'version': info['version'], 'uptime_s': info['uptime_s'], 'rssi': info['wifi']['rssi'],
                        'ip': info['wifi']['ip'], 'mem_free': info['mem_free']})


def state_payload(motion, clear_after_s):
    """motion: DeviceAPI.motion_info(). What the sensor sees, and its settings."""
    return ujson.dumps({'motion': motion['active'], 'last': motion['last'], 'count': motion['count'],
                        'enabled': motion['enabled'], 'clear_after_s': clear_after_s})


def parse_set(payload):
    """A command from the set topic (JSON text), as sensor settings for
    DeviceAPI.save_config, which checks their values. Raises ValueError."""
    command = ujson.loads(payload)
    if not isinstance(command, dict) or not command:
        raise ValueError('a command must be a JSON object, such as {"enabled": false}')
    for key in command:
        if key not in SETTABLE:
            raise ValueError('there is no setting ' + str(key))
    if 'enabled' in command and not isinstance(command['enabled'], bool):
        raise ValueError('enabled must be true or false')
    v = command.get('clear_after_s')
    if isinstance(v, float) and v == int(v):  # 20.0, from a slider
        command['clear_after_s'] = int(v)
    return command
