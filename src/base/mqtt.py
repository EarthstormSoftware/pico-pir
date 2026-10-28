"""
MQTT: the device's state and commands, and Home Assistant discovery, as PlasmaStick's, so home automation sees every
Earthstorm device the same way.

The topics hang off mqtt.topic_base ({name} is the device name) and the discovery prefix, mqtt.ha_prefix:

  <base>/status        retained: online, or offline (the will)
  <base>/info          retained: the health figures from /api/info, every minute
  <base>/state         retained, lights: {"State", "Mode", "ModeParameters", "Brightness"}, on every change
  <base>/control       lights: {"MessageType": "StateChange" | "ModeChange" | "BrightnessChange", "Parameters": {...}}
  <base>/ha/state      retained, lights: the state in Home Assistant's JSON light schema
  <base>/ha/set        lights: Home Assistant's JSON light commands
  <base>/alert         lights: starts an alert (if alerts are switched on): empty, or {"Colour", "Seconds", "OnMs",
                       "OffMs", "Brightness"} for other than the alert settings; {"Active": false} ends one
  <base>/alert/set     lights: {"Enabled": true | false} switches alerts on or off
  <base>/alert/state   retained, lights: {"Enabled", "Active"}
  <base>/state         retained, other products: the product's state(device), when it calls device.state_changed()
  <base>/set           other products: commands, as a JSON object, for the product's set(device, command)
  <ha_prefix>/light/<name>/config, <ha_prefix>/sensor/<name>/<key>/config, and for alerts .../switch/<name>/alerts
                       and .../button/<name>/alert: retained discovery (if it's on)
  <ha_prefix>/<component>/<name>/<key>/config   the same for a product's own entities: its HA_ENTITIES, as
                       (component, key), each described by its ha_entity(device, key, topics)

Groups (mqtt.group): lights with the same group name follow each other through a retained topic,
earthstorm/group/<group>, carrying {"State", "Mode", "ModeParameters", "Brightness", "From"}. Each member publishes its
own changes there (not the ones it makes following the group), and one that starts or joins takes the group's state.
A member follows power, and brightness unless mqtt.group_brightness is off, and the mode only if it has it. Until
PlasmaStick moves to App Base, it uses plasmastick/group/<group>: lights here publish to both and listen to both.

mqtt.alert_topic, if set, is another device's topic to watch: a message on it starts an alert (see watched()), so a
motion sensor can ring the light like a doorbell. The product can listen to topics of its own (mqtt_topics(device)
and mqtt_message(device, topic, text)).

Retained topics the device stops using (after a topic setting changes) are cleared when it next connects.

The client is the platform's (platform.mqtt_client): base.mqttclient on a Pi, mqtt_as on a Pico.
"""
from base.compat import asyncio, json
from base.log import Log
from base.settings import Setting, is_bool, is_text

TAG = 'MQTT'
ONLINE = 'online'
OFFLINE = 'offline'
DEFAULT_TOPIC_BASE = '/home/device/{name}'
DEFAULT_HA_PREFIX = 'homeassistant'
DEFAULT_PORT = 1883
INFO_INTERVAL_S = 60
MANUFACTURER = 'Earthstorm Software'
# Home Assistant gives colour temperatures in mireds (a million over kelvin): 6500 K to 2000 K
MIN_MIREDS, MAX_MIREDS = 153, 500

# The health figures Home Assistant shows as diagnostic sensors, where the platform has them: (key, name, config)
SENSORS = (
    ('uptime_s', 'Uptime', {'device_class': 'duration', 'unit_of_measurement': 's',
                            'state_class': 'total_increasing'}),
    ('temperature_c', 'Temperature', {'device_class': 'temperature', 'unit_of_measurement': '°C',
                                      'state_class': 'measurement'}),
    ('load', 'Load', {'state_class': 'measurement', 'icon': 'mdi:gauge'}),
    ('mem_free', 'Free memory', {'device_class': 'data_size', 'unit_of_measurement': 'B',
                                 'state_class': 'measurement'}),
    ('rssi', 'Wi-Fi signal', {'device_class': 'signal_strength', 'unit_of_measurement': 'dBm',
                              'state_class': 'measurement'}),
    ('ip', 'IP address', {}),
)


def _prefix_error(value, placeholder):
    if not isinstance(value, str) or not value:
        return "can't be empty"
    if len(value) > 100:
        return 'must be at most 100 characters'
    if '+' in value or '#' in value:
        return "can't contain the wildcards + or #"
    if '\0' in value:
        return "can't contain a null character"
    if value != value.strip():
        return "can't start or end with a space"
    if value.endswith('/'):
        return "can't end with /"
    if '//' in value:
        return "can't contain an empty level (//)"
    rest = value.replace('{name}', '') if placeholder else value
    if '{' in rest or '}' in rest:
        return 'only understands the placeholder {name}' if placeholder else "can't contain { or }"
    return None


def topic_base_error(value):
    return _prefix_error(value, True)


def ha_prefix_error(value):
    return _prefix_error(value, False)


def broker_error(value):
    if not isinstance(value, str) or len(value) > 100 or value != value.strip() or ' ' in value:
        return 'must be a host name or address, optionally with :port'
    try:
        broker(value)
    except ValueError as e:
        return str(e)
    return None


def broker(address):
    """(host, port) from 'host', 'host:port', or '[IPv6]:port'."""
    host, port = address, DEFAULT_PORT
    if address.startswith('['):
        host, _, rest = address[1:].partition(']')
        if rest:
            port = rest[1:]
    elif address.count(':') == 1:
        host, port = address.split(':')
    try:
        port = int(port)
    except ValueError:
        raise ValueError('has a port that isn\'t a number')
    if not 0 < port < 65536:
        raise ValueError('has a port outside 1-65535')
    return host, port


# Changing any of them reconnects with the new ones, through a restart
MQTT_SETTINGS = {
    'mqtt': {
        'enabled': Setting(False, is_bool, restart=True),
        'broker_address': Setting('', broker_error, restart=True),
        'user': Setting('', is_text(64), restart=True),
        # Kept as given (the broker needs it), but never shown
        'password': Setting('', is_text(64, allow_empty=False), restart=True, secret=True),
        'topic_base': Setting(DEFAULT_TOPIC_BASE, topic_base_error, restart=True),
        'ha_discovery': Setting(False, is_bool, restart=True),
        'ha_prefix': Setting(DEFAULT_HA_PREFIX, ha_prefix_error, restart=True),
    },
}

# A light's, next to those
LIGHT_MQTT_SETTINGS = {
    'mqtt': {
        # A group to follow ('' for none), and whether to follow its brightness
        'group': Setting('', lambda value: group_error(value), restart=True),
        'group_brightness': Setting(True, is_bool, restart=True),
        # Another device's topic whose messages start an alert ('' for none)
        'alert_topic': Setting('', lambda value: watch_topic_error(value), restart=True),
    },
}


GROUP_PREFIX = 'earthstorm/group/'
# PlasmaStick's, until it's on App Base
OLD_GROUP_PREFIX = 'plasmastick/group/'
_GROUP_CHARS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-'
# The mode parameters a group passes on
_GROUP_PARAMETERS = ('Colour', 'Kelvin', 'Speed', 'Style')
GROUP_SETTLE_MS = 300


def group_error(value):
    if not isinstance(value, str) or len(value) > 32 or any(c not in _GROUP_CHARS for c in value):
        return 'must be up to 32 letters, digits, - or _'
    return None


def group_payload(light, name):
    """The group's new state after a change here: the light's state, with the parameters that define the mode."""
    state = light.state()
    parameters = {k: v for k, v in state['ModeParameters'].items() if k in _GROUP_PARAMETERS}
    if 'Kelvin' in parameters:
        parameters.pop('Colour', None)  # the temperature says it
    return json.dumps({'State': state['State'], 'Mode': state['Mode'], 'ModeParameters': parameters,
                       'Brightness': state['Brightness'], 'From': name})


def group_change(payload, light, follow_brightness):
    """What following the group's state changes, as keyword arguments for Light.set: only what differs, so a light's
    own message coming back changes nothing, and an animation isn't started again. A mode the light doesn't have is
    left out (it keeps its own). Raises ValueError for a state it can't use."""
    if not isinstance(payload, dict):
        raise ValueError('a group state must be a JSON object')
    state, mode, brightness = payload.get('State'), payload.get('Mode'), payload.get('Brightness')
    parameters = payload.get('ModeParameters') or {}
    if not isinstance(state, bool):
        raise ValueError('State must be true or false')
    if isinstance(brightness, bool) or not isinstance(brightness, int) or not 1 <= brightness <= 100:
        raise ValueError('Brightness must be 1-100')
    if not isinstance(parameters, dict):
        raise ValueError('ModeParameters must be an object')
    change = {}
    if state != light.on:
        change['on'] = state
    if mode in light.modes:
        wanted = {k: v for k, v in parameters.items() if k in _GROUP_PARAMETERS}
        if 'Kelvin' in wanted:
            wanted.pop('Colour', None)
        if light.parameters_error(mode, wanted):
            wanted = {}
        differs = any(str(v).lower() != str(light.parameters.get(k)).lower() for k, v in wanted.items())
        if mode != light.mode or differs:
            change['mode'] = mode
            change['parameters'] = wanted or None
    if follow_brightness and brightness != light.brightness:
        change['brightness'] = brightness
    return change


def watch_topic_error(value):
    """For a topic the device subscribes to: wildcards are allowed."""
    if not isinstance(value, str) or len(value) > 200 or '\0' in value or value != value.strip():
        return 'must be a topic, up to 200 characters'
    return None


def topics(name, base=DEFAULT_TOPIC_BASE, ha_prefix=DEFAULT_HA_PREFIX, light=False, sensors=(), group='',
           product_state=False, entities=()):
    """The device's topics by role. entities: a product's Home Assistant entities, as (component, key)."""
    base = base.replace('{name}', name)
    t = {'status': base + '/status', 'info': base + '/info'}
    if product_state:
        t.update({'state': base + '/state', 'set': base + '/set'})
    for component, key in entities:
        t['ha_' + key] = ha_prefix + '/' + component + '/' + name + '/' + key + '/config'
    if light:
        t.update({
            'state': base + '/state',
            'control': base + '/control',
            'ha_state': base + '/ha/state',
            'ha_set': base + '/ha/set',
            'ha_light': ha_prefix + '/light/' + name + '/config',
            'alert': base + '/alert',
            'alert_set': base + '/alert/set',
            'alert_state': base + '/alert/state',
            'ha_alerts': ha_prefix + '/switch/' + name + '/alerts/config',
            'ha_alert': ha_prefix + '/button/' + name + '/alert/config',
        })
        if group:
            # Not retained by this light alone (the others use it too), so never cleared
            t['group'] = GROUP_PREFIX + group
            t['group_old'] = OLD_GROUP_PREFIX + group
    for key in sensors:
        t['ha_sensor_' + key] = ha_prefix + '/sensor/' + name + '/' + key + '/config'
    return t


def retained(t):
    """The topics in t that the device publishes retained."""
    return sorted(topic for role, topic in t.items()
                  if role not in ('control', 'ha_set', 'alert', 'alert_set', 'group', 'group_old', 'set'))


# Payloads

def info_payload(info):
    """The health figures from /api/info that the info topic carries."""
    payload = {'version': info['version']}
    for key, _, _ in SENSORS:
        if key in info:
            payload[key] = info[key]
    return json.dumps(payload)


def ha_state_payload(state):
    """state: Light.state(). The same in Home Assistant's JSON light schema."""
    parameters = state['ModeParameters']
    payload = {'state': 'ON' if state['State'] else 'OFF', 'color_mode': 'rgb', 'effect': state['Mode'],
               'brightness': state['Brightness']}
    kelvin = parameters.get('Kelvin')
    if kelvin:
        payload['color_mode'] = 'color_temp'
        payload['color_temp'] = (1000000 + kelvin // 2) // kelvin
    colour = parameters.get('Colour')
    if colour:
        digits = colour.lstrip('#')
        payload['color'] = {'r': int(digits[0:2], 16), 'g': int(digits[2:4], 16), 'b': int(digits[4:6], 16)}
    return json.dumps(payload)


def _device(name, product):
    d = {'identifiers': [name], 'name': name, 'manufacturer': getattr(product, 'MANUFACTURER', MANUFACTURER),
         'model': getattr(product, 'MODEL', product.NAME), 'sw_version': product.VERSION}
    return d


def ha_light_payload(name, t, product, modes):
    return json.dumps({
        'name': None,  # the entity takes the device's name
        'unique_id': name + '_light',
        'schema': 'json',
        'command_topic': t['ha_set'],
        'state_topic': t['ha_state'],
        'availability_topic': t['status'],
        'supported_color_modes': ['rgb', 'color_temp'],
        'min_mireds': MIN_MIREDS,
        'max_mireds': MAX_MIREDS,
        'brightness': True,
        'brightness_scale': 100,  # percent, as the device has it
        'effect': True,
        'effect_list': list(modes),
        'device': _device(name, product),
    })


def alert_state_payload(light):
    return json.dumps({'Enabled': light.settings.get('alert', 'enabled'), 'Active': light.alerting})


def ha_alerts_payload(name, t, product):
    """The switch that turns alerts on and off."""
    return json.dumps({
        'name': 'Alerts', 'unique_id': name + '_alerts', 'icon': 'mdi:alarm-light-outline',
        'command_topic': t['alert_set'], 'payload_on': '{"Enabled": true}', 'payload_off': '{"Enabled": false}',
        'state_topic': t['alert_state'], 'value_template': "{{ 'ON' if value_json.Enabled else 'OFF' }}",
        'state_on': 'ON', 'state_off': 'OFF', 'availability_topic': t['status'], 'device': _device(name, product),
    })


def ha_alert_payload(name, t, product):
    """The button that starts an alert (when they're switched on)."""
    return json.dumps({
        'name': 'Alert', 'unique_id': name + '_alert', 'icon': 'mdi:alarm-light', 'command_topic': t['alert'],
        'payload_press': '{}', 'availability_topic': t['status'], 'device': _device(name, product),
    })


def parse_alert(text):
    """A message on the alert topic, as keyword arguments for Light.alert, or None to end the alert."""
    if not text.strip():
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError('an alert must be empty or a JSON object')
    if payload.get('Active') is False:
        return None
    names = {'Colour': 'colour', 'Seconds': 'seconds', 'OnMs': 'on_ms', 'OffMs': 'off_ms', 'Brightness': 'brightness'}
    return {names[k]: v for k, v in payload.items() if k in names}


_OFF = ('', '0', 'off', 'false', 'no', 'clear', 'idle', 'closed', 'inactive')


def watched(text):
    """Whether a message on the watched topic means "now": a JSON object with "motion" (Pico PIR's state),
    "State" or "state" says so itself; true or ON does; false, OFF, 0 or empty doesn't; any other text does."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text.strip().lower() not in _OFF
    if isinstance(payload, dict):
        for key in ('motion', 'State', 'state', 'occupancy'):
            if key in payload:
                value = payload[key]
                return value.strip().lower() not in _OFF if isinstance(value, str) else bool(value)
        return True
    if isinstance(payload, str):
        return payload.strip().lower() not in _OFF
    return bool(payload)


def topic_matches(pattern, topic):
    """Whether topic is one that the subscription pattern (with + and # wildcards) receives."""
    wanted, parts = pattern.split('/'), topic.split('/')
    for i, level in enumerate(wanted):
        if level == '#':
            return True
        if i >= len(parts) or (level != '+' and level != parts[i]):
            return False
    return len(parts) == len(wanted)


def ha_entity_payload(name, t, product, key, config):
    """A product's own entity: its config, with what every entity has (read from the state topic unless it says)."""
    full = {'unique_id': name + '_' + key, 'state_topic': t.get('state'), 'availability_topic': t['status'],
            'device': _device(name, product)}
    full.update(config)
    return json.dumps(full)


def parse_set(text):
    """A command on the set topic: a JSON object. Raises ValueError."""
    command = json.loads(text)
    if not isinstance(command, dict) or not command:
        raise ValueError('a command must be a JSON object, such as {"enabled": false}')
    return command


def ha_sensor_payload(name, t, product, key):
    for k, title, extra in SENSORS:
        if k == key:
            config = {
                'name': title,
                'unique_id': name + '_' + key,
                'state_topic': t['info'],
                'value_template': '{{ value_json.' + key + ' }}',
                'availability_topic': t['status'],
                'entity_category': 'diagnostic',
                'device': _device(name, product),
            }
            config.update(extra)
            return json.dumps(config)
    raise ValueError('unknown sensor')


# Commands, as keyword arguments for Light.set

def _number(value, low, high, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError('{} must be {}-{}'.format(what, low, high))
    return value


def parse_control(payload):
    """A control message (PlasmaStick's): StateChange, ModeChange or BrightnessChange. Raises ValueError."""
    if not isinstance(payload, dict):
        raise ValueError('a control message must be a JSON object')
    kind = payload.get('MessageType')
    parameters = payload.get('Parameters') or {}
    if not isinstance(parameters, dict):
        raise ValueError('Parameters must be an object')
    if kind == 'StateChange':
        if not isinstance(parameters.get('State'), bool):
            raise ValueError('State must be true or false')
        return {'on': parameters['State']}
    if kind == 'ModeChange':
        return {'mode': parameters.get('Mode'), 'parameters': parameters.get('ModeParameters')}
    if kind == 'BrightnessChange':
        return {'brightness': parameters.get('Brightness')}
    raise ValueError('unknown MessageType {}'.format(kind))


def parse_ha_command(payload, modes):
    """A Home Assistant JSON light command. Raises ValueError for anything it can't use."""
    if not isinstance(payload, dict):
        raise ValueError('a command must be a JSON object')
    change = {}
    colour, effect = payload.get('color'), payload.get('effect')
    if colour is not None:
        try:
            r, g, b = (int(colour[k]) for k in ('r', 'g', 'b'))
        except (KeyError, TypeError, ValueError):
            raise ValueError('color needs r, g and b')
        if not all(0 <= v <= 255 for v in (r, g, b)):
            raise ValueError('color values must be 0-255')
        change.update(mode='Colour', parameters={'Colour': '#{:02x}{:02x}{:02x}'.format(r, g, b)})
    elif payload.get('color_temp') is not None:
        mireds = _number(payload['color_temp'], 100, 1000, 'color_temp')
        change.update(mode='Colour', parameters={'Kelvin': min(6500, max(2000, int(1000000 / mireds + 0.5)))})
    elif effect is not None:
        if effect not in modes:
            raise ValueError('unknown effect')
        change['mode'] = effect
    if payload.get('brightness') is not None:
        change['brightness'] = max(1, int(_number(payload['brightness'], 0, 100, 'brightness') + 0.5))
    state = payload.get('state')
    if state is not None:
        if state not in ('ON', 'OFF'):
            raise ValueError('state must be ON or OFF')
        change['on'] = state == 'ON'
    if not change:
        raise ValueError('nothing to do')
    if payload.get('transition') is not None:
        change['transition_ms'] = int(_number(payload['transition'], 0, 3600, 'transition') * 1000 + 0.5)
    return change


def failure(e):
    """Why connecting failed, for the page."""
    if isinstance(e, asyncio.TimeoutError):
        return "The broker didn't answer in time."
    if isinstance(e, ConnectionRefusedError):
        return 'The broker refused the connection: is it running, and on that port?'
    text = str(e) or e.__class__.__name__
    if 'getaddrinfo' in text or 'Name or service' in text or 'nodename' in text:
        return "The broker's address wasn't found."
    return 'Could not connect: ' + text[:1].upper() + text[1:] + ('' if text.endswith('.') else '.')


class Mqtt:
    def __init__(self, device):
        self.device = device
        s = device.settings
        self.enabled = s.get('mqtt', 'enabled') and bool(s.get('mqtt', 'broker_address'))
        self.connected = False
        self.error = None
        self.name = device.platform.device_name()
        self.ha = s.get('mqtt', 'ha_discovery')
        self.light = device.light
        # The health figures the platform has: as it says (a Pico's Wi-Fi figures are empty until it's connected), or
        # those it gives now
        self.sensors = getattr(device.platform, 'SENSORS', None)
        if self.sensors is None:
            info = device.info()
            self.sensors = tuple(key for key, _, _ in SENSORS if info.get(key) is not None)
        self.group = s.get('mqtt', 'group') if self.light is not None else ''
        product = device.product
        self.product_state = self.light is None and hasattr(product, 'state')
        self.entities = getattr(product, 'HA_ENTITIES', ())
        self.topics = topics(self.name, s.get('mqtt', 'topic_base'), s.get('mqtt', 'ha_prefix'),
                             light=self.light is not None, sensors=self.sensors, group=self.group,
                             product_state=self.product_state, entities=self.entities)
        self.alert_topic = s.get('mqtt', 'alert_topic') if self.light is not None else ''
        self._watched_before = False  # whether the watched topic last said "now", so one that stays "now" rings once
        self._changed = asyncio.Event()
        self._group_changed = asyncio.Event()
        if self.light is not None:
            self.light.listeners.append(self._light_changed)

    def _light_changed(self, light):
        self._changed.set()
        if light.local and self.group:
            self._group_changed.set()

    def status(self):
        return {'enabled': self.enabled, 'connected': self.connected, 'error': self.error}

    async def run(self):
        if not self.enabled:
            return
        s = self.device.settings
        host, port = broker(s.get('mqtt', 'broker_address'))
        wait = 1
        while True:
            client = self.device.platform.mqtt_client(
                client_id=self.name, host=host, port=port, user=s.get('mqtt', 'user'),
                password=s.reveal('mqtt', 'password'), will=(self.topics['status'], OFFLINE, True))
            tasks = []
            try:
                await client.connect()
                self.connected, self.error, wait = True, None, 1
                Log.info(TAG, 'Connected to {}:{}'.format(host, port))
                await self._up(client)
                tasks = [asyncio.create_task(self._publish_state(client)),
                         asyncio.create_task(self._publish_info(client)),
                         asyncio.create_task(self._publish_group(client))]
                await self._receive(client)
                self.error = 'The connection to the broker dropped.'
            except Exception as e:  # anything a broker, or the network, can do: try again
                self.error = failure(e)
            finally:
                self.connected = False
                for task in tasks:
                    task.cancel()
                client.close()
            Log.warn(TAG, '{} Trying again in {} s'.format(self.error, wait))
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)

    async def _up(self, client):
        t = self.topics
        await client.publish(t['status'], ONLINE, retain=True)
        await self._clear_retired(client)
        # With discovery off, an empty retained config removes any earlier one from Home Assistant
        if self.light is not None:
            await client.subscribe(t['control'])
            if self.ha:
                await client.subscribe(t['ha_set'])
            await client.subscribe(t['alert'])
            await client.subscribe(t['alert_set'])
            if self.alert_topic:
                await client.subscribe(self.alert_topic)
            product = self.device.product
            for role, payload in (('ha_light', lambda: ha_light_payload(self.name, t, product, self.light.modes)),
                                  ('ha_alerts', lambda: ha_alerts_payload(self.name, t, product)),
                                  ('ha_alert', lambda: ha_alert_payload(self.name, t, product))):
                await client.publish(t[role], payload() if self.ha else '', retain=True)
        if self.product_state:
            await client.subscribe(t['set'])
        for component, key in self.entities:
            await client.publish(t['ha_' + key], ha_entity_payload(
                self.name, t, self.device.product, key, self.device.product.ha_entity(self.device, key, t))
                if self.ha else '', retain=True)
        for topic in self._product_topics():
            await client.subscribe(topic)
        if self.group:
            # The group's retained state arrives straight away, and the light takes it: PlasmaStick's topic second,
            # as it's the one they use until they're on App Base
            await client.subscribe(t['group'])
            await client.subscribe(t['group_old'])
        for key in self.sensors:
            await client.publish(t['ha_sensor_' + key], ha_sensor_payload(self.name, t, self.device.product, key)
                                 if self.ha else '', retain=True)
        await self._send_info(client)
        self._changed.set()  # the state, now it's connected

    async def _clear_retired(self, client):
        """Empties retained topics the device used before and doesn't now (a topic setting changed)."""
        path = self.device.platform.state_path('mqtt-topics.json')
        current = retained(self.topics)
        try:
            with open(path) as f:
                before = json.loads(f.read())
        except (OSError, ValueError):
            before = []
        retired = [topic for topic in before if topic not in current]
        for topic in retired:
            await client.publish(topic, '', retain=True)
        if retired:
            Log.info(TAG, 'Cleared {} old retained topic(s)'.format(len(retired)))
        if before != current:
            from base.compat import write_atomically
            write_atomically(path, json.dumps(current))

    def _product_topics(self):
        product = self.device.product
        return product.mqtt_topics(self.device) if hasattr(product, 'mqtt_topics') else []

    def alert_switched(self):
        """Publishes the alert state again (the switch changed on the page)."""
        self._changed.set()

    async def _send_info(self, client):
        await client.publish(self.topics['info'], info_payload(self.device.info()), retain=True)

    # A publish that fails closes the connection, so run() connects again

    async def _publish_info(self, client):
        try:
            while True:
                await asyncio.sleep(INFO_INTERVAL_S)
                await self._send_info(client)
        except OSError:
            client.close()

    async def _publish_group(self, client):
        """Passes changes made here to the group, as its new state: a moment after each, so dragging a slider makes
        one message rather than dozens."""
        if not self.group:
            return
        self._group_changed.clear()  # what happened while disconnected: the group's state wins on joining
        try:
            while True:
                await self._group_changed.wait()
                await asyncio.sleep(GROUP_SETTLE_MS / 1000)
                self._group_changed.clear()
                payload = group_payload(self.light, self.name)
                await client.publish(self.topics['group'], payload, retain=True)
                await client.publish(self.topics['group_old'], payload, retain=True)
        except OSError:
            client.close()

    def state_changed(self):
        """The product's state changed (or its settings): publish it."""
        self._changed.set()

    async def _publish_state(self, client):
        if self.product_state:
            try:
                while True:
                    await self._changed.wait()
                    self._changed.clear()
                    await client.publish(self.topics['state'], json.dumps(self.device.product.state(self.device)),
                                         retain=True)
            except OSError:
                client.close()
            return
        if self.light is None:
            return
        try:
            while True:
                await self._changed.wait()
                self._changed.clear()
                state = self.light.state()
                await client.publish(self.topics['state'], json.dumps(state), retain=True)
                if self.ha:
                    await client.publish(self.topics['ha_state'], ha_state_payload(state), retain=True)
                await client.publish(self.topics['alert_state'], alert_state_payload(self.light), retain=True)
        except OSError:
            client.close()

    async def _receive(self, client):
        while True:
            message = await client.message()
            if message is None:
                return
            topic, text, retained = message
            Log.debug(TAG, '{}: {}'.format(topic, text))
            try:
                error = self._handle(topic, text, retained)
            except (ValueError, AttributeError) as e:
                error = str(e)
            if error:
                Log.warn(TAG, 'Ignoring a message on {}: {}'.format(topic, error))

    def _handle(self, topic, text, retained):
        """Acts on a message; returns why it couldn't, or None. Raises ValueError for one it can't read."""
        t, light = self.topics, self.light
        if topic == t.get('set'):
            if retained:  # it would be done again on every connection, undoing later changes
                return 'commands should not be retained'
            return self.device.product.set(self.device, parse_set(text)) if hasattr(self.device.product, 'set') \
                else None
        if topic == t.get('control'):
            return light.set(**parse_control(json.loads(text)))
        if topic == t.get('ha_set'):
            return light.set(**parse_ha_command(json.loads(text), light.modes))
        if topic == t.get('alert'):
            if retained:  # left on the broker by someone: not an event now
                return None
            alert = parse_alert(text)
            if alert is None:
                light.stop_alert()
                return None
            return light.alert(**alert)
        if topic == t.get('alert_set'):
            enabled = json.loads(text).get('Enabled')
            if not isinstance(enabled, bool):
                return 'Enabled must be true or false'
            error, changed = self.device.settings.update({'alert': {'enabled': enabled}})
            if not error:
                self.device.applied(changed)
            self._changed.set()  # the switch's state, even if it didn't change
            return error
        if topic in (t.get('group'), t.get('group_old')):
            change = group_change(json.loads(text), light, self.device.settings.get('mqtt', 'group_brightness'))
            return light.set(local=False, **change) if change else None
        if self.alert_topic and topic_matches(self.alert_topic, topic):
            now = watched(text)
            ring = now and not self._watched_before and not retained
            self._watched_before = now
            if ring and self.device.settings.get('alert', 'enabled'):
                return light.alert()
            return None
        if hasattr(self.device.product, 'mqtt_message'):
            self.device.product.mqtt_message(self.device, topic, text)
        return None
