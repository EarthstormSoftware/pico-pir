import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import new_device  # noqa: E402

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'device-template.example.json')


@pytest.mark.parametrize('name, ok', [('Hallway', True), ('Stairs-Top_2', True), ('x' * 32, True),
                                      ('', False), ('x' * 33, False), ('Living Room', False), ('a/b', False),
                                      ('a+b', False)])
def test_device_names(name, ok):
    assert (new_device.name_error(name) is None) == ok


def test_the_example_template_builds_a_config_with_overrides():
    template = new_device.read_template(EXAMPLE)
    config = new_device.build_config(template, 'Hallway', [
        ('device', 'pir_pin', 15), ('sensor', 'clear_after_s', None)])
    assert config['device']['device_name'] == 'Hallway' and config['mqtt']['user'] == 'Hallway'
    assert config['device']['pir_pin'] == 15  # overridden
    assert config['sensor']['clear_after_s'] == 15  # None leaves the template's value
    assert config['mqtt']['enabled'] is True and config['mqtt']['ha_discovery'] is True
    assert 'password' not in json.dumps(config)  # passwords are added on the board, encrypted
    assert template['device'] is not config['device']  # the template itself is left alone


def test_the_built_config_passes_the_firmwares_checks(tmp_path, monkeypatch):
    from api import DeviceAPI
    from deviceconfig import DeviceConfig
    monkeypatch.chdir(tmp_path)
    config = new_device.build_config(new_device.read_template(EXAMPLE), 'Hallway', [])
    api = DeviceAPI(DeviceConfig.loadConfigFromFile(), pico_led=None)
    device = {k: v for k, v in config['device'].items() if k != 'device_name'}
    mqtt = {k: v for k, v in config['mqtt'].items() if k != 'user'}
    assert api.save_config({'device': device, 'sensor': config['sensor'], 'mqtt': mqtt}) == (True, None)


def test_the_built_config_boots_as_written(tmp_path, monkeypatch):
    # Written straight to config.json by setup, so it has to load without falling back to defaults.
    from deviceconfig import DeviceConfig
    monkeypatch.chdir(tmp_path)
    config = new_device.build_config(new_device.read_template(EXAMPLE), 'Hallway', [('device', 'pir_pin', 15)])
    (tmp_path / 'config.json').write_text(json.dumps(config))
    loaded = DeviceConfig.loadConfigFromFile()
    assert loaded.pir_pin == 15 and loaded.sensorconfig.clear_after_s == 15
    assert loaded.mqttconfig.user == 'Hallway' and loaded.mqttconfig.ha_discovery


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI is Windows only')
def test_wifi_details_are_kept_encrypted_for_this_user(tmp_path, monkeypatch):
    monkeypatch.setattr(new_device, 'SECRETS_DIR', str(tmp_path))
    monkeypatch.setattr(new_device, 'WIFI_FILE', str(tmp_path / 'wifi.dpapi'))
    new_device.save_wifi('MyNetwork', 'correct horse battery staple')
    raw = (tmp_path / 'wifi.dpapi').read_bytes()
    assert b'staple' not in raw and b'MyNetwork' not in raw
    assert new_device.load_wifi() == {'ssid': 'MyNetwork', 'password': 'correct horse battery staple'}
