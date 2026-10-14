"""Home Assistant discovery: the configs that announce the device's sensors.
Only needed while connecting, so app.py imports it then and lets it go again.
"""
import ujson
from product import MANUFACTURER, MODEL
from deviceconfig import CLEAR_AFTER_MIN_S, CLEAR_AFTER_MAX_S

# Home Assistant diagnostic sensors, read from the info topic: (key, name, extra config).
SENSORS = (
    ('rssi', 'Wi-Fi signal', {'device_class': 'signal_strength', 'unit_of_measurement': 'dBm',
                              'state_class': 'measurement'}),
    ('uptime_s', 'Uptime', {'device_class': 'duration', 'unit_of_measurement': 's',
                            'state_class': 'total_increasing'}),
    ('mem_free', 'Free memory', {'device_class': 'data_size', 'unit_of_measurement': 'B',
                                 'state_class': 'measurement'}),
    ('ip', 'IP address', {}),
)


def device(client_id, sw_version=None):
    """The device block every entity carries, so Home Assistant groups them under one device."""
    d = {'identifiers': [client_id], 'name': client_id, 'manufacturer': MANUFACTURER, 'model': MODEL}
    if sw_version:
        d['sw_version'] = sw_version
    return d


def ha_sensor_discovery_payload(client_id, t, key, sw_version=None):
    """Discovery config for one of SENSORS. t: the device's topics() dict."""
    for k, name, extra in SENSORS:
        if k == key:
            config = {
                'name': name,
                'unique_id': client_id + '_' + key,
                'state_topic': t['info'],
                'value_template': '{{ value_json.' + key + ' }}',
                'availability_topic': t['status'],
                'entity_category': 'diagnostic',
                'device': device(client_id, sw_version),
            }
            config.update(extra)
            return ujson.dumps(config)
    raise ValueError('unknown sensor')


def ha_entity_discovery_payload(client_id, t, key, sw_version=None):
    """Discovery config for one of mqttstate.HA_ENTITIES, read from the state
    topic; the settings are changed through the set topic. No room is given:
    Home Assistant asks for one."""
    config = {
        'unique_id': client_id + '_' + key,
        'state_topic': t['state'],
        'availability_topic': t['status'],
        'device': device(client_id, sw_version),
    }
    if key == 'motion':
        config.update({'name': 'Motion', 'device_class': 'motion',
                       'value_template': "{{ 'ON' if value_json.motion else 'OFF' }}"})
    elif key == 'last_motion':
        # null until the clock is set, which Home Assistant shows as unknown.
        config.update({'name': 'Last motion', 'device_class': 'timestamp', 'value_template': '{{ value_json.last }}'})
    elif key == 'clear_after_s':
        config.update({'name': 'Clear after', 'device_class': 'duration', 'unit_of_measurement': 's',
                       'min': CLEAR_AFTER_MIN_S, 'max': CLEAR_AFTER_MAX_S, 'step': 1, 'mode': 'box',
                       'entity_category': 'config', 'value_template': '{{ value_json.clear_after_s }}',
                       'command_topic': t['set'], 'command_template': '{"clear_after_s": {{ value | int }}}'})
    elif key == 'sensing':
        config.update({'name': 'Sensing', 'icon': 'mdi:motion-sensor', 'entity_category': 'config',
                       'value_template': "{{ 'ON' if value_json.enabled else 'OFF' }}",
                       'state_on': 'ON', 'state_off': 'OFF', 'command_topic': t['set'],
                       'payload_on': '{"enabled": true}', 'payload_off': '{"enabled": false}'})
    else:
        raise ValueError('unknown entity')
    return ujson.dumps(config)
