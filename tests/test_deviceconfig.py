import json

import pytest

from deviceconfig import DeviceConfig, _merge_with_defaults, _redact_passwords
from deviceidentity import sanitize_device_name


@pytest.fixture
def device_dir(tmp_path, monkeypatch):
    """An empty device filesystem as the current directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def test_missing_keys_and_sections_come_from_defaults():
    merged = _merge_with_defaults({'device': {'log_level': 2}, 'wlan': 'not a dict', 'extra': {'k': 1}})
    assert merged['device']['log_level'] == 2
    assert merged['device']['ntp_server'] == 'pool.ntp.org'
    assert merged['wlan']['connect_timeout'] == 30
    assert merged['mqtt']['queue_len'] == 1
    assert merged['extra'] == {'k': 1}  # unknown sections are kept


def test_partial_config_file_still_loads(device_dir):
    write(device_dir / 'config.json', {'device': {'dst': 'eu'}})
    cfg = DeviceConfig.loadConfigFromFile()
    assert cfg.dst == 'eu'
    assert cfg.watchdog is True
    assert cfg.wlanconfig.reconnect_wait == 30


@pytest.mark.parametrize('contents', [None, '{not json', '[1, 2]'])
def test_missing_or_bad_config_file_uses_defaults(device_dir, contents):
    if contents is not None:
        write(device_dir / 'config.json', contents)
    cfg = DeviceConfig.loadConfigFromFile()
    assert cfg.log_level == 1
    assert cfg.device_name == 'PicoPIR-A1B2C3'  # generated, not PicoPIR-UNKNOWN


def test_device_name_priority(device_dir):
    write(device_dir / 'config.json', {'device': {'device_name': 'FromConfig'}})
    assert DeviceConfig.loadConfigFromFile().device_name == 'FromConfig'
    write(device_dir / 'provisioning.json', {'ssid': 'Home', 'password': 'pw', 'device_name': 'FromSetup'})
    assert DeviceConfig.loadConfigFromFile().device_name == 'FromSetup'
    write(device_dir / 'device_name.json', {'name': 'Renamed'})
    assert DeviceConfig.loadConfigFromFile().device_name == 'Renamed'


def test_provisioned_ssid_beats_config_json(device_dir):
    write(device_dir / 'config.json', {'wlan': {'ssid': 'Old', 'password': 'x'}})
    assert DeviceConfig.loadConfigFromFile().wlanconfig.ssid == 'Old'
    write(device_dir / 'provisioning.json', {'ssid': 'New', 'password': 'pw'})
    assert DeviceConfig.loadConfigFromFile().wlanconfig.ssid == 'New'


def test_provisioning_needed_until_credentials_are_saved(device_dir):
    assert DeviceConfig.provisioning_needed()
    write(device_dir / 'provisioning.json', {'ssid': 'Home'})
    assert DeviceConfig.provisioning_needed()
    write(device_dir / 'provisioning.json', {'ssid': 'Home', 'password_iv': 'aa', 'password_enc': 'bb'})
    assert not DeviceConfig.provisioning_needed()


def test_setup_request_is_taken_once(device_dir):
    assert not DeviceConfig.take_setup_request()
    DeviceConfig.request_setup_mode()
    assert DeviceConfig.take_setup_request()
    assert not DeviceConfig.take_setup_request()


def test_passwords_are_masked_for_logging():
    safe = _redact_passwords({'wlan': {'ssid': 'Home', 'password': 'secret'}, 'mqtt': {'password': ''}})
    assert safe == {'wlan': {'ssid': 'Home', 'password': '***'}, 'mqtt': {'password': ''}}


@pytest.mark.parametrize('raw, expected', [
    ('Stairs PIR', 'Stairs-PIR'),
    ('  -Hall_1!@#- ', 'Hall_1'),
    ('', 'PicoPIR-A1B2C3'),
    ('!!!', 'PicoPIR-A1B2C3'),
])
def test_device_names_are_sanitised(raw, expected):
    assert sanitize_device_name(raw) == expected


def test_log_level_defaults_to_info_and_rejects_bad_values(device_dir):
    assert DeviceConfig.loadConfigFromFile().log_level == 1
    write(device_dir / 'config.json', {'device': {'log_level': 2}})
    assert DeviceConfig.loadConfigFromFile().log_level == 2
    write(device_dir / 'config.json', {'device': {'log_level': 'loud'}})
    assert DeviceConfig.loadConfigFromFile().log_level == 1


def test_bad_time_settings_fall_back(device_dir):
    write(device_dir / 'config.json', {'device': {'utc_offset_min': 7, 'dst': 'mars'}})
    cfg = DeviceConfig.loadConfigFromFile()
    assert (cfg.utc_offset_min, cfg.dst) == (0, 'none')


def _setup_files(device_dir):
    write(device_dir / 'config.json', {
        'device': {'log_level': 2},
        'mqtt': {'broker_address': 'mqtt.local', 'password': 'secret'},
        'web': {'password': 'letmein'},
    })
    write(device_dir / 'provisioning.json', {'ssid': 'Home', 'password': 'pw'})
    write(device_dir / 'device_name.json', {'name': 'Stairs'})


def test_reset_settings_keeps_wifi(device_dir):
    _setup_files(device_dir)
    DeviceConfig.reset(keep_wifi=True)
    assert json.loads((device_dir / 'config.json').read_text()) == {'device': {}}
    assert (device_dir / 'provisioning.json').exists()
    assert not (device_dir / 'device_name.json').exists()
    cfg = DeviceConfig.loadConfigFromFile()
    assert cfg.log_level == 1 and cfg.web_password_hash == '' and cfg.mqttconfig.broker_address == ''


def test_factory_reset_also_forgets_wifi(device_dir):
    _setup_files(device_dir)
    DeviceConfig.reset(keep_wifi=False)
    assert not (device_dir / 'provisioning.json').exists()
    assert DeviceConfig.provisioning_needed()


def test_reset_settings_keeps_wifi_saved_in_config_json(device_dir):
    write(device_dir / 'config.json', {'wlan': {'ssid': 'Home', 'password': 'pw', 'reconnect_wait': 99}})
    DeviceConfig.reset(keep_wifi=True)
    assert json.loads((device_dir / 'config.json').read_text())['wlan'] == {'ssid': 'Home', 'password': 'pw'}
