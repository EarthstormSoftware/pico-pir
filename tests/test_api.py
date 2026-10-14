import json

import pytest

import mqttstate
from api import DeviceAPI
from deviceconfig import DeviceConfig


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps({'device': {'dst': 'eu'}}))
    cfg = DeviceConfig.loadConfigFromFile()
    return DeviceAPI(cfg, pico_led=None)


def saved_config(api):
    with open('config.json') as fp:
        return json.load(fp)


def test_save_writes_changes_on_top_of_the_merged_config(api):
    assert api.save_config({'device': {'log_level': 2}, 'mqtt': {'broker_address': 'mqtt.local'}}) == (True, None)
    saved = saved_config(api)
    assert saved['device']['log_level'] == 2
    assert saved['device']['dst'] == 'eu'
    assert saved['device']['ntp_server'] == 'pool.ntp.org'  # filled in from defaults
    assert saved['mqtt']['broker_address'] == 'mqtt.local'


@pytest.mark.parametrize('updates, error', [
    ({'device': {'log_level': 3}}, 'log_level must be 0, 1 or 2'),
    ({'device': {'utc_offset_min': 61}}, 'utc_offset_min must be -720 to 840, in steps of 15'),
    ({'device': {'dst': 'au'}}, 'dst must be none, eu or us'),
    ({'wlan': {'connect_timeout': 1}}, 'connect_timeout must be >= 5'),
    ({'mqtt': {'password': ''}}, 'MQTT password cannot be empty'),
    ({'mqtt': {'queue_len': 0}}, 'queue_len must be >= 1'),
    ({'mqtt': {'topic_base': 'home/#'}}, 'topic_base cannot contain the wildcards + or #'),
    ({'mqtt': {'ha_prefix': ''}}, 'ha_prefix cannot be empty'),
    ({'device': {'pir_pin': 29}}, 'pir_pin must be a GPIO number, 0-28'),
    ({'sensor': {'clear_after_s': 4}}, 'clear_after_s must be 5-3600 seconds'),
    ({'sensor': {'clear_after_s': '30'}}, 'clear_after_s must be 5-3600 seconds'),
])
def test_invalid_updates_are_rejected(api, updates, error):
    assert api.save_config(updates) == (False, error)


def test_a_rejected_save_leaves_nothing_half_applied(api):
    ok, _ = api.save_config({'device': {'log_level': 2, 'dst': 'au'}})
    assert not ok
    api.save_config({'mqtt': {'user': 'me'}})
    assert saved_config(api)['device']['log_level'] == 1


def test_config_view_hides_passwords(api):
    api.save_config({'mqtt': {'password': 'secret'}})
    assert 'secret' not in json.dumps(api.get_config())


def test_device_name_rules(api):
    assert api.set_device_name('Stairs PIR')
    assert api.device_name == 'Stairs-PIR'
    assert not api.set_device_name('')
    assert not api.set_device_name('x' * 33)


def test_info_reports_health(api):
    info = api.get_device_info()
    assert info['version']
    assert info['uptime_s'] >= 0
    assert info['wifi'] == {'ssid': '', 'ip': '192.168.4.1', 'rssi': None}  # not connected
    assert info['mqtt'] == {'enabled': False, 'connected': False, 'error': None}
    assert info['mem_free'] == 120000


def test_info_reports_mqtt_problems(api):
    class Client:
        def isconnected(self):
            return False
    api.mqtt_client = Client()
    api.mqtt_error = "(-1, 'CONNACK reason code 0x5'). Check the MQTT user and password."
    assert api.get_device_info()['mqtt'] == {'enabled': True, 'connected': False, 'error': api.mqtt_error}


def test_live_settings_apply_without_a_restart(api):
    from log import Log
    assert api.save_config({'device': {'log_level': 2, 'utc_offset_min': 60, 'dst': 'us'}}) == (True, None)
    assert Log.level == 2
    assert (api.device_config.utc_offset_min, api.device_config.dst) == (60, 'us')
    assert not api.restart_required()
    api.save_config({'device': {'log_level': 1}})
    assert Log.level == 1


@pytest.mark.parametrize('updates', [
    {'device': {'watchdog': False}},
    {'device': {'ntp_server': 'time.local'}},
    {'wlan': {'reconnect_wait': 45}},
    {'mqtt': {'broker_address': 'mqtt.local'}},
    {'mqtt': {'topic_base': 'sensors/{name}'}},
    {'mqtt': {'ha_prefix': 'ha'}},
])
def test_boot_only_settings_need_a_restart(api, updates):
    api.save_config(updates)
    assert api.restart_required()


def test_saving_unchanged_boot_settings_needs_no_restart(api):
    api.save_config({'device': {'watchdog': True}, 'wlan': {'reconnect_wait': api.device_config.wlanconfig.reconnect_wait}})
    assert not api.restart_required()


def test_a_rename_needs_a_restart(api):
    assert api.set_device_name('stairs')
    assert api.restart_required()


def test_topic_settings_are_shown_and_saved_trimmed(api):
    mq = api.get_config()['mqtt']
    assert (mq['topic_base'], mq['ha_prefix']) == ('/home/device/{name}', 'homeassistant')
    assert api.save_config({'mqtt': {'topic_base': ' sensors/{name} ', 'ha_prefix': 'ha'}}) == (True, None)
    assert saved_config(api)['mqtt']['topic_base'] == 'sensors/{name}'
    assert api.get_config()['mqtt']['topic_base'] == 'sensors/{name}'


def test_changing_topics_retires_the_old_retained_ones(api):
    running = api.device_config.mqttconfig.topics()
    api.save_config({'mqtt': {'user': 'me'}})
    assert DeviceConfig.retired_topics() == []
    api.save_config({'mqtt': {'topic_base': 'sensors/{name}'}})
    assert DeviceConfig.retired_topics() == [running['status'], running['info']]


def test_a_rename_retires_the_old_retained_topics(api):
    running = api.device_config.mqttconfig.topics()
    assert api.set_device_name('stairs')
    assert running['ha_sensor_rssi'] in DeviceConfig.retired_topics()


@pytest.mark.parametrize('keep_wifi', [True, False])
def test_a_reset_retires_the_retained_topics_in_use(api, keep_wifi):
    running = api.device_config.mqttconfig.topics()
    api.reset(keep_wifi=keep_wifi)
    assert DeviceConfig.retired_topics() == [running[k] for k in mqttstate.RETAINED]


def test_a_config_save_can_rename_the_device(api):
    assert api.save_config({'device': {'device_name': ' Stairs PIR ', 'log_level': 2}}) == (True, None)
    assert api.device_name == 'Stairs-PIR'
    assert api.restart_required()
    # The name is kept in device_name.json, not config.json.
    assert saved_config(api)['device']['device_name'] == ''
    with open('device_name.json') as fp:
        assert json.load(fp) == {'name': 'Stairs-PIR'}


def test_saving_the_same_name_is_not_a_rename(api):
    assert api.save_config({'device': {'device_name': api.device_name}}) == (True, None)
    assert not api.restart_required()


@pytest.mark.parametrize('name', ['', '   ', 'x' * 33])
def test_a_bad_name_rejects_the_whole_save(api, name):
    assert api.save_config({'device': {'device_name': name, 'log_level': 2}}) == (
        False, 'device_name must be 1-32 characters')
    assert saved_config(api)['device'].get('log_level', 1) == 1


def test_every_settings_field_on_the_page_is_a_config_setting(api):
    import os
    import re
    page_file = os.path.join(os.path.dirname(__file__), '..', 'src', 'www', 'index.html')
    page = open(page_file, encoding='utf-8').read()
    names = re.findall(r'name="((?:device|sensor|wlan|mqtt|web)\.[\w.]+)"', page)
    assert len(names) > 15
    cfg = api.get_config()
    write_only = {'web.password', 'web.clear_password', 'mqtt.password'}
    for name in names:
        if name in write_only:
            continue
        value = cfg
        for key in name.split('.'):
            assert isinstance(value, dict) and key in value, name
            value = value[key]


def test_the_page_knows_which_settings_apply_live():
    import os
    import re
    from api import LIVE_DEVICE_KEYS
    page_file = os.path.join(os.path.dirname(__file__), '..', 'src', 'www', 'index.html')
    live = re.search(r'var LIVE = \[([^\]]*)\]', open(page_file, encoding='utf-8').read()).group(1)
    on_page = {name.split('.', 1)[1] for name in re.findall(r"'(device\.[\w.]+)'", live)}
    assert on_page == set(LIVE_DEVICE_KEYS)
    # The sensor's settings all apply at once.
    page = open(page_file, encoding='utf-8').read()
    sensor_fields = set(re.findall(r'name="(sensor\.\w+)"', page))
    assert sensor_fields == {'sensor.enabled', 'sensor.clear_after_s'}
    assert sensor_fields <= set(re.findall(r"'(sensor\.\w+)'", live))


def test_watchdog_setting_needs_a_restart(api):
    assert api.get_config()['device']['watchdog'] is True
    assert api.save_config({'device': {'watchdog': False}}) == (True, None)
    assert saved_config(api)['device']['watchdog'] is False
    assert api.restart_required()
    assert api.get_device_info()['watchdog_stall'] is None


def test_the_time_is_only_reported_once_the_clock_is_set(api):
    assert api.get_device_info()['time'] is None
    api.clock_set = True
    t = api.get_device_info()['time']
    assert len(t) == 25 and t[4] == '-' and t[10] == 'T' and t.endswith('+00:00')
    api.utc_offset = 7200  # as app.py sets it for UTC+1 with summer time
    assert api.get_device_info()['time'].endswith('+02:00')


def test_uptime_survives_the_clock_being_set(api):
    before = api.get_device_info()['uptime_s']
    api._started -= 5 * 365 * 86400  # as if the clock had just jumped five years forward...
    api.shift_clock(5 * 365 * 86400)  # ...and app.py passed the jump on
    assert api.get_device_info()['uptime_s'] - before < 2


@pytest.mark.parametrize('server, ok', [('pool.ntp.org', True), ('192.168.1.1', True), ('', True),
                                        ('bad host', False), ('x' * 65, False), ('a/b', False)])
def test_ntp_server_is_checked(api, server, ok):
    assert api.save_config({'device': {'ntp_server': server}})[0] == ok


def test_the_settings_code_is_let_go_after_use(api):
    import sys
    api.get_config()
    api.save_config({'device': {'log_level': 1}})
    assert 'settings' not in sys.modules


def test_sensor_settings_apply_without_a_restart(api):
    assert api.get_config()['sensor'] == {'enabled': True, 'clear_after_s': 15}
    assert api.save_config({'sensor': {'enabled': False, 'clear_after_s': 120}}) == (True, None)
    cfg = api.device_config.sensorconfig
    assert (cfg.enabled, cfg.clear_after_s) == (False, 120)
    assert saved_config(api)['sensor']['clear_after_s'] == 120
    assert not api.restart_required()


def test_the_sensor_pin_needs_a_restart_and_survives_a_reset(api):
    assert api.get_config()['device']['pir_pin'] == 22
    assert api.save_config({'device': {'pir_pin': 15}}) == (True, None)
    assert api.restart_required()
    api.reset(keep_wifi=True)
    assert DeviceConfig.loadConfigFromFile().pir_pin == 15


def test_info_reports_motion(api):
    assert api.get_device_info()['motion'] is None  # no sensor

    class Sensor:
        def info(self):
            return {'enabled': True, 'settling': False, 'active': True, 'count': 3, 'last': 1700000000,
                    'recent': [[1700000000, None], [1699999000, 1699999040]]}
    api.sensor = Sensor()
    motion = api.get_device_info()['motion']
    assert motion['last'] is None and motion['recent'] == [[None, None], [None, None]]  # the clock isn't set yet
    api.clock_set = True
    api.utc_offset = 3600
    assert api.get_device_info()['motion'] == {
        'enabled': True, 'settling': False, 'active': True, 'count': 3, 'last': '2023-11-14T23:13:20+01:00',
        'recent': [['2023-11-14T23:13:20+01:00', None], ['2023-11-14T22:56:40+01:00', '2023-11-14T22:57:20+01:00']]}
