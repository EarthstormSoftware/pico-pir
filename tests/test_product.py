"""Pico PIR on App Base: the same topics and Home Assistant entities as the firmware before it (ha_v1.json is what
v1.0.0 published for a device called hall), so Home Assistant carries on as it was; and the set topic's commands."""
import json
import os

import pytest

import product
from base import discovery
from base import mqtt, pico

HERE = os.path.dirname(os.path.abspath(__file__))


def v1():
    with open(os.path.join(HERE, 'ha_v1.json'), encoding='utf-8') as f:
        return json.load(f)


def topics():
    return mqtt.topics('hall', sensors=pico.SENSORS, product_state=True, entities=product.HA_ENTITIES)


def as_v1(config):
    """A discovery config with v1.0.0's version, to compare."""
    config = json.loads(config)
    config['device']['sw_version'] = '1.0.0'
    return config


def test_the_topics_are_v1s():
    # 1.0's, unchanged, and the log topic 2.0 adds
    t = topics()
    assert t.pop('log') == '/home/device/hall/log'
    assert t == v1()['topics']


def test_the_sensor_entities_are_v1s():
    t = topics()
    for key in pico.SENSORS:
        assert as_v1(discovery.sensor('hall', t, product, key)) == v1()['ha_sensor_' + key], key


def test_the_motion_entities_are_v1s():
    t = topics()
    for _, key in product.HA_ENTITIES:
        config = discovery.entity('hall', t, product, key, product.ha_entity(None, key, t))
        assert as_v1(config) == v1()['ha_' + key], key


class Settings:
    def __init__(self):
        self.values = {'enabled': True, 'clear_after_s': 15}

    def update(self, updates):
        values = updates['sensor']
        if 'clear_after_s' in values and not isinstance(values['clear_after_s'], int):
            return 'sensor.clear_after_s must be a whole number', set()
        self.values.update(values)
        return None, {('sensor', key) for key in values}


class Device:
    def __init__(self):
        self.settings = Settings()
        self.changed = []

    def applied(self, changed):
        self.changed.append(changed)


def test_commands_change_the_sensor_settings():
    device = Device()
    assert product.set(device, {'enabled': False, 'clear_after_s': 20.0}) is None
    assert device.settings.values == {'enabled': False, 'clear_after_s': 20}
    assert device.changed == [{('sensor', 'enabled'), ('sensor', 'clear_after_s')}]


@pytest.mark.parametrize('command', [{'pir_pin': 3}, {'clear_after_s': 'soon'}])
def test_other_commands_are_refused(command):
    device = Device()
    assert product.set(device, command)
    assert device.settings.values == {'enabled': True, 'clear_after_s': 15}


def test_schedule_events_switch_sensing():
    device = Device()
    product.scheduled(device, False)
    assert device.settings.values['enabled'] is False
    product.scheduled(device, True)
    assert device.settings.values['enabled'] is True
    assert device.changed == [{('sensor', 'enabled')}, {('sensor', 'enabled')}]
