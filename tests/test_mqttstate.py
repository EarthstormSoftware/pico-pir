import json

import pytest

import mqttha
import mqttstate
from deviceconfig import DeviceConfig


def test_topics():
    t = mqttstate.topics('Stairs')
    assert t['status'] == '/home/device/Stairs/status'
    assert t['info'] == '/home/device/Stairs/info'
    assert t['ha_sensor_rssi'] == 'homeassistant/sensor/Stairs/rssi/config'


def test_default_config_uses_the_default_topics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = DeviceConfig.loadConfigFromFile()
    assert cfg.mqttconfig.topics() == mqttstate.topics(cfg.mqttconfig.client_id)
    assert cfg.mqttconfig.topics()['status'] == '/home/device/' + cfg.mqttconfig.client_id + '/status'


def test_custom_topics():
    t = mqttstate.topics('Stairs', 'sensors/{name}', 'ha')
    assert t['status'] == 'sensors/Stairs/status'
    assert t['ha_sensor_ip'] == 'ha/sensor/Stairs/ip/config'
    # A base without {name} suits a single device.
    assert mqttstate.topics('Stairs', 'stairs')['info'] == 'stairs/info'


@pytest.mark.parametrize('base', ['/home/device/{name}', 'pir/{name}', 'stairs', 'a/{name}/b', 'my sensors/{name}'])
def test_good_topic_bases(base):
    assert mqttstate.topic_base_error(base) is None


@pytest.mark.parametrize('base, error', [
    ('', 'topic_base cannot be empty'),
    ('x' * 101, 'topic_base must be at most 100 characters'),
    ('home/+/{name}', 'topic_base cannot contain the wildcards + or #'),
    ('home/#', 'topic_base cannot contain the wildcards + or #'),
    ('home\0', 'topic_base cannot contain a null character'),
    (' home', 'topic_base cannot start or end with a space'),
    ('home/{name}/', 'topic_base cannot end with /'),
    ('home//{name}', 'topic_base cannot contain an empty level (//)'),
    ('home/{id}', 'topic_base only understands the placeholder {name}'),
])
def test_bad_topic_bases(base, error):
    assert mqttstate.topic_base_error(base) == error


def test_ha_prefix_rules():
    assert mqttstate.ha_prefix_error('homeassistant') is None
    assert mqttstate.ha_prefix_error('ha/{name}') == 'ha_prefix cannot contain { or }'
    assert mqttstate.ha_prefix_error('') == 'ha_prefix cannot be empty'


def test_hand_edited_unusable_topics_fall_back_to_the_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps({'mqtt': {'topic_base': 'bad/#', 'ha_prefix': ''}}))
    cfg = DeviceConfig.loadConfigFromFile()
    assert cfg.mqttconfig.topics() == mqttstate.topics(cfg.mqttconfig.client_id)


def test_retired_topics_are_the_retained_ones_no_longer_used():
    old = mqttstate.topics('Stairs')
    assert mqttstate.retired_topics(old, old) == []
    # The discovery configs stay where they were, so they aren't cleared.
    assert mqttstate.retired_topics(old, mqttstate.topics('Stairs', 'sensors/{name}')) == [
        '/home/device/Stairs/status', '/home/device/Stairs/info']
    renamed = mqttstate.retired_topics(old, mqttstate.topics('Hall'))
    assert 'homeassistant/sensor/Stairs/rssi/config' in renamed


def test_retired_topics_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert DeviceConfig.retired_topics() == []
    DeviceConfig.retire_topics(['a/info', 'a/status'])
    DeviceConfig.retire_topics(['a/status', 'b/info'])
    assert DeviceConfig.retired_topics() == ['a/info', 'a/status', 'b/info']
    DeviceConfig.clear_retired_topics()
    assert DeviceConfig.retired_topics() == []
    DeviceConfig.clear_retired_topics()  # nothing to clear is fine


def test_mqtt_will_marks_the_device_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import sys
    import types
    monkeypatch.setitem(sys.modules, 'mqtt_as', types.SimpleNamespace(config={}))
    cfg = DeviceConfig.loadConfigFromFile()
    assert cfg.get_mqtt_as_config()['will'] == (mqttstate.topics(cfg.mqttconfig.client_id)['status'], 'offline', True, 0)


def test_info_payload_has_the_health_figures():
    info = {'version': '0.1.0', 'uptime_s': 125, 'mem_free': 86000,
            'wifi': {'ssid': 'Home', 'ip': '192.168.1.20', 'rssi': -61}}
    assert json.loads(mqttstate.info_payload(info)) == {
        'version': '0.1.0', 'uptime_s': 125, 'rssi': -61, 'ip': '192.168.1.20', 'mem_free': 86000}


@pytest.mark.parametrize('key', list(mqttstate.SENSOR_KEYS))
def test_sensor_discovery_reads_the_info_topic(key):
    t = mqttstate.topics('Stairs')
    config = json.loads(mqttha.ha_sensor_discovery_payload('Stairs', t, key, '0.1.0'))
    assert config['state_topic'] == t['info'] and config['availability_topic'] == t['status']
    assert config['value_template'] == '{{ value_json.' + key + ' }}'
    assert config['unique_id'] == 'Stairs_' + key
    assert config['entity_category'] == 'diagnostic'
    # Each carries the whole device, so Home Assistant can create it from any of them.
    assert config['device'] == {'identifiers': ['Stairs'], 'name': 'Stairs', 'manufacturer': 'Raspberry Pi',
                                'model': 'Pico W', 'sw_version': '0.1.0'}
    # Every sensor reads a figure the info payload has.
    info = {'version': 'x', 'uptime_s': 1, 'mem_free': 1, 'wifi': {'ip': 'x', 'rssi': -1}}
    assert key in json.loads(mqttstate.info_payload(info))


def test_unknown_sensor():
    with pytest.raises(ValueError):
        mqttha.ha_sensor_discovery_payload('Stairs', mqttstate.topics('Stairs'), 'nope')


def test_the_sensor_descriptions_match_the_sensor_topics():
    assert tuple(s[0] for s in mqttha.SENSORS) == mqttstate.SENSOR_KEYS
