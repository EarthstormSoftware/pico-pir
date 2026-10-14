"""Home Assistant discovery: the configs that announce the device's sensors.
Only needed while connecting, so app.py imports it then and lets it go again.
"""
import ujson
from product import MANUFACTURER, MODEL

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
