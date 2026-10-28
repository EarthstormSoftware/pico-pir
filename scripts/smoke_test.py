"""Smoke-test a running device over HTTP and MQTT (host-side, not deployed).

Checks the HTTP API and web UI, and changing the sensor's settings through
/api/config. Then, over MQTT, what the device publishes (the retained status, info
and state topics and, with Home Assistant discovery on, the discovery configs),
and that commands on the set topic change the settings while bad ones are
ignored. With --wave it also asks for someone to wave at the sensor (any time
once asked: the checks carry on meanwhile), and checks that the motion and the
clear arrive on the state topic. The sensor's settings are put back at the end.

The broker address, user and device name come from the device's /api/config.
That omits the password, so it is read from a local config.json (gitignored),
the PICOPIR_MQTT_PASSWORD environment variable (or PLASMASTICK_MQTT_PASSWORD, for
a test account the two share), or --mqtt-password.

    py scripts/smoke_test.py --host Hallway.local
    py scripts/smoke_test.py --host 192.168.1.50 --mqtt-user tester --wave
    py scripts/smoke_test.py --host 192.168.1.50 --soak 300

During a soak the device is also pinged every second. A failed or slow poll while
pings were being lost is reported as a network blip (Wi-Fi or access point) and
doesn't fail the soak; one while pings got through means the firmware stalled, and does.

Prints PASS/FAIL/SKIP lines; exits 1 if anything failed, 2 if the device can't be reached.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import base64
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []


def report(status, name, detail=''):
    results.append(status)
    print(f'{status} {name}' + (f' ({detail})' if detail else ''), flush=True)


class Device:
    def __init__(self, host, timeout, password=None):
        self.base = f'http://{host}'
        self.timeout = timeout
        self.password = password

    def request(self, method, path, body=None, headers=None):
        """Return (status, parsed JSON or text, headers, seconds)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if self.password:
            token = base64.b64encode(f'smoke-test:{self.password}'.encode()).decode()
            req.add_header('Authorization', f'Basic {token}')
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw, headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), e.headers
        elapsed = time.monotonic() - start
        text = raw.decode('utf-8', errors='replace')
        try:
            return status, json.loads(text), headers, elapsed
        except ValueError:
            return status, text, headers, elapsed

    def info(self):
        status, body, _, _ = self.request('GET', '/api/info')
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f'/api/info returned {status}')
        return body

    def sensor(self):
        """The sensor's settings, from /api/config."""
        status, body, _, _ = self.request('GET', '/api/config')
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f'/api/config returned {status}')
        return body.get('sensor', {})

    def wait_for_sensor(self, expected, timeout=5.0):
        """Poll /api/config until the sensor settings include expected. Returns (ok, last settings)."""
        deadline = time.monotonic() + timeout
        settings = {}
        while time.monotonic() < deadline:
            settings = self.sensor()
            if all(settings.get(k) == v for k, v in expected.items()):
                return True, settings
            time.sleep(0.25)
        return False, settings


def check(name, ok, detail=''):
    report('PASS' if ok else 'FAIL', name, detail)
    return ok


def contains_key(obj, needle):
    if isinstance(obj, dict):
        # web.password_set only says whether one is set, so it doesn't count.
        return any((needle in k.lower() and not k.lower().endswith('_set')) or contains_key(v, needle)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return any(contains_key(v, needle) for v in obj)
    return False


def test_http(dev):
    status, body, _, elapsed = dev.request('GET', '/api/info')
    check('http.info', status == 200 and isinstance(body, dict) and {'name', 'version', 'motion'} <= body.keys(),
          f'{status} in {elapsed * 1000:.0f} ms')
    motion = body.get('motion') if isinstance(body, dict) else None
    check('http.info_motion', isinstance(motion, dict) and
          {'enabled', 'settling', 'active', 'count', 'last', 'recent'} <= motion.keys(), f'motion={motion}')

    status, body, headers, _ = dev.request('GET', '/')
    check('http.webui', status == 200 and 'text/html' in headers.get('Content-Type', ''), f'{status}')

    status, config, _, _ = dev.request('GET', '/api/config')
    check('http.config', status == 200 and isinstance(config, dict) and 'sensor' in config, f'{status}')
    if isinstance(config, dict):
        check('http.config_hides_passwords', not contains_key(config, 'password'))

    status, _, _, _ = dev.request('GET', '/no-such-page')
    check('http.not_found', status == 404, f'{status}')

    before = dev.sensor()
    status, _, _, _ = dev.request('POST', '/api/config', {'sensor': {'enabled': not before.get('enabled')}},
                                  headers={'Origin': 'http://evil.example'})
    check('http.cross_site_refused', status == 403 and dev.sensor() == before, f'{status}')

    password_set = isinstance(config, dict) and config.get('web', {}).get('password_set')
    if password_set:
        status, _, headers, _ = Device(dev.base[len('http://'):], dev.timeout).request('GET', '/api/info')
        check('http.password_required', status == 401 and 'Basic' in headers.get('WWW-Authenticate', ''), f'{status}')
    else:
        report('SKIP', 'http.password_required', 'no web password set on the device')

    # The sensor's settings apply at once: no restart.
    clear = before.get('clear_after_s', 15)
    other = clear + 1 if clear < 3600 else clear - 1
    status, body, _, _ = dev.request('POST', '/api/config', {'sensor': {'clear_after_s': other}})
    ok, settings = dev.wait_for_sensor({'clear_after_s': other})
    check('http.clear_after', status == 200 and ok and body.get('restart_required') is False,
          f'clear_after_s={settings.get("clear_after_s")}, restart_required={body.get("restart_required")}')

    for enabled in (False, True):
        status, _, _, _ = dev.request('POST', '/api/config', {'sensor': {'enabled': enabled}})
        ok, _ = dev.wait_for_sensor({'enabled': enabled})
        motion = dev.info().get('motion') or {}
        check(f'http.sensing_{"on" if enabled else "off"}', status == 200 and ok and motion.get('enabled') is enabled,
              f'motion.enabled={motion.get("enabled")}' + ('' if enabled else f', active={motion.get("active")}'))

    for bad in ({'clear_after_s': 2}, {'clear_after_s': 3601}, {'clear_after_s': '30'}):
        status, body, _, _ = dev.request('POST', '/api/config', {'sensor': bad})
        check(f'http.rejected_{bad["clear_after_s"]!r}', status == 400 and dev.sensor().get('clear_after_s') == other,
              f'{status} {body.get("error") if isinstance(body, dict) else body}')

    dev.request('POST', '/api/config', {'sensor': {'clear_after_s': clear}})
    return config if isinstance(config, dict) else {}


def load_mqtt_password(cli_password):
    if cli_password is not None:
        return cli_password
    for name in ('PICOPIR_MQTT_PASSWORD', 'PLASMASTICK_MQTT_PASSWORD'):
        if os.environ.get(name):
            return os.environ[name]
    try:
        with open(os.path.join(PROJECT_ROOT, 'config.json'), encoding='utf-8') as fp:
            return json.load(fp).get('mqtt', {}).get('password') or None
    except (OSError, ValueError):
        return None


WAVE_WAIT_S = 120

HA_ENTITIES = {'motion': 'binary_sensor', 'last_motion': 'sensor', 'clear_after_s': 'number', 'sensing': 'switch'}


def test_mqtt(dev, config, args):
    mqtt = config.get('mqtt', {})
    if not mqtt.get('enabled') or not mqtt.get('broker_address'):
        report('SKIP', 'mqtt', 'MQTT is disabled or has no broker on the device')
        return
    try:
        import paho.mqtt.client as paho
    except ImportError:
        report('FAIL', 'mqtt', 'paho-mqtt is not installed: py -m pip install -r requirements-dev.txt')
        return

    broker = args.broker or mqtt['broker_address']
    device_name = dev.info()['name']  # the name in use (config.json's device_name is only a fallback)
    base = mqtt.get('topic_base', '/home/device/{name}').replace('{name}', device_name)
    ha_prefix = mqtt.get('ha_prefix', 'homeassistant')
    set_topic = base + '/set'
    ha = bool(mqtt.get('ha_discovery'))
    watched = {'status': base + '/status', 'state': base + '/state', 'info': base + '/info'}
    if ha:
        watched['ha_sensor'] = f'{ha_prefix}/sensor/{device_name}/rssi/config'
        for key, component in HA_ENTITIES.items():
            watched['ha_' + key] = f'{ha_prefix}/{component}/{device_name}/{key}/config'
    seen = {}  # topic -> latest payload text
    states = []  # every state message, in order, for --wave

    def on_message(_c, _u, m):
        text = m.payload.decode('utf-8', 'replace')
        seen[m.topic] = text
        if m.topic == watched['state']:
            try:
                states.append(json.loads(text))
            except ValueError:
                pass

    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f'smoke-test-{os.getpid()}')
    client.on_message = on_message
    # The test's own login: a device with an account of its own (whose password
    # only the device has) is tested through another account, --mqtt-user.
    user = args.mqtt_user or mqtt.get('user')
    if user:
        client.username_pw_set(user, load_mqtt_password(args.mqtt_password))
    try:
        client.connect(broker, args.mqtt_port, keepalive=30)
    except OSError as e:
        report('FAIL', 'mqtt.connect', f'{broker}:{args.mqtt_port}: {e}')
        return
    client.loop_start()
    try:
        deadline = time.monotonic() + 10
        while not client.is_connected() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not check('mqtt.connect', client.is_connected(), f'{broker}:{args.mqtt_port} as {user}, base {base}'):
            report('FAIL', 'mqtt', 'broker refused the connection (check user/password)')
            return
        client.subscribe([(t, 0) for t in watched.values()])
        if args.wave:
            motion = dev.info()['motion']
            count = motion['count']
            if motion['settling']:
                print('WAIT the sensor is still settling after power-on (its first minute): a wave before then '
                      "doesn't count", flush=True)
            print('WAVE at the sensor any time from now, then keep out of its view until motion clears '
                  f'(the test waits up to {WAVE_WAIT_S} s for it at the end)', flush=True)

        def wait_json(name, test, timeout=5.0):
            """Wait for the watched topic's latest JSON payload to pass test. Returns (ok, payload)."""
            deadline = time.monotonic() + timeout
            payload = None
            while time.monotonic() < deadline:
                try:
                    payload = json.loads(seen.get(watched[name], ''))
                    if test(payload):
                        return True, payload
                except ValueError:
                    pass
                time.sleep(0.2)
            return False, payload

        # Retained, so these arrive as soon as we subscribe.
        deadline = time.monotonic() + 5
        while watched['status'] not in seen and time.monotonic() < deadline:
            time.sleep(0.1)
        status = seen.get(watched['status'])
        check('mqtt.status_online', status == 'online', f'status={status}')
        expected_info = dev.info()
        ok, info = wait_json('info', lambda p: p.get('version') == expected_info.get('version')
                             and isinstance(p.get('uptime_s'), int) and p.get('ip') == expected_info['ip'])
        check('mqtt.info', ok and isinstance(info.get('mem_free'), int), f'info={info}')
        settings = dev.sensor()
        ok, state = wait_json('state', lambda p: p.get('enabled') == settings.get('enabled') and
                              p.get('clear_after_s') == settings.get('clear_after_s'))
        check('mqtt.state', ok and isinstance(state.get('motion'), bool) and isinstance(state.get('count'), int),
              f'state={state}')
        if ha:
            ok, sensor = wait_json('ha_sensor', lambda c: c.get('unique_id') == f'{device_name}_rssi')
            check('mqtt.ha_sensor_discovery', ok and sensor.get('state_topic') == watched['info'],
                  f'state_topic={sensor and sensor.get("state_topic")}')
            for key in HA_ENTITIES:
                ok, entity = wait_json('ha_' + key, lambda c: c.get('unique_id') == f'{device_name}_{key}')
                detail = entity and {k: entity.get(k) for k in ('state_topic', 'command_topic', 'device_class')}
                ok = ok and entity.get('state_topic') == watched['state'] and \
                    entity.get('availability_topic') == watched['status']
                if key in ('clear_after_s', 'sensing'):
                    ok = ok and entity.get('command_topic') == set_topic
                check(f'mqtt.ha_{key}_discovery', ok, f'{detail}')
        else:
            report('SKIP', 'mqtt.ha_discovery', 'Home Assistant discovery is off on the device')

        def publish(payload):
            text = payload if isinstance(payload, str) else json.dumps(payload)
            # Never retained, so nothing is left on the broker for the device to replay.
            client.publish(set_topic, text, qos=1, retain=False).wait_for_publish(5)

        clear = settings.get('clear_after_s', 15)
        other = clear + 2 if clear < 3599 else clear - 2
        publish({'clear_after_s': other})
        ok, value = dev.wait_for_sensor({'clear_after_s': other})
        check('mqtt.set_clear_after', ok, f'clear_after_s={value.get("clear_after_s")}')
        ok, state = wait_json('state', lambda p: p.get('clear_after_s') == other)
        check('mqtt.state_follows_clear_after', ok, f'state={state}')

        for enabled in (False, True):
            publish({'enabled': enabled})
            ok, value = dev.wait_for_sensor({'enabled': enabled})
            check(f'mqtt.set_sensing_{"on" if enabled else "off"}', ok, f'enabled={value.get("enabled")}')
            ok, state = wait_json('state', lambda p: p.get('enabled') is enabled)
            check(f'mqtt.state_follows_sensing_{"on" if enabled else "off"}', ok and (enabled or not state['motion']),
                  f'state={state}')

        # Home Assistant's own commands, as its discovery configs describe them.
        if ha:
            number = json.loads(seen.get(watched['ha_clear_after_s'], '{}'))
            template = number.get('command_template', '')
            if '{{ value | int }}' in template:
                client.publish(set_topic, template.replace('{{ value | int }}', str(clear)), qos=1).wait_for_publish(5)
                ok, value = dev.wait_for_sensor({'clear_after_s': clear})
                check('mqtt.ha_number_command', ok, f'clear_after_s={value.get("clear_after_s")}')
            else:
                report('FAIL', 'mqtt.ha_number_command', f'unexpected command_template {template!r}')
            switch = json.loads(seen.get(watched['ha_sensing'], '{}'))
            for payload, enabled in ((switch.get('payload_off'), False), (switch.get('payload_on'), True)):
                client.publish(set_topic, payload or '', qos=1).wait_for_publish(5)
                ok, value = dev.wait_for_sensor({'enabled': enabled})
                check(f'mqtt.ha_switch_{"on" if enabled else "off"}', ok, f'enabled={value.get("enabled")}')

        # Bad commands must be ignored without changing anything or breaking the handler.
        expected = dev.sensor()
        for bad in ('this is not json', '{}', '[1]', {'brightness': 5}, {'enabled': 'yes'},
                    {'clear_after_s': 2}, {'clear_after_s': 99999}):
            publish(bad)
        time.sleep(1.5)
        check('mqtt.bad_commands_ignored', dev.sensor() == expected, f'sensor={dev.sensor()}')

        publish({'clear_after_s': other})
        ok, _ = dev.wait_for_sensor({'clear_after_s': other})
        check('mqtt.still_handling_after_bad_commands', ok)

        if args.wave:
            def detected():
                # The first motion published since the wave was asked for (count only goes up on motion).
                return next((i for i, p in enumerate(states) if p.get('motion') is True and p.get('count', 0) > count),
                            None)

            def cleared(start):
                # A clear after it, with sensing on: turning sensing off clears too, but isn't the sensor.
                return any(p.get('motion') is False and p.get('enabled') is True for p in states[start + 1:])

            deadline = time.monotonic() + WAVE_WAIT_S
            if detected() is None or not cleared(detected()):
                print(f'WAITING for the wave and the clear (up to {WAVE_WAIT_S} s)', flush=True)
            while time.monotonic() < deadline and (detected() is None or not cleared(detected())):
                time.sleep(0.5)
            first = detected()
            check('mqtt.motion', first is not None and states[first].get('last') is not None,
                  f'state={states[first] if first is not None else (states[-1] if states else None)}')
            if first is not None:
                check('mqtt.motion_cleared', cleared(first), f'state={states[-1]}')
    finally:
        client.loop_stop()
        client.disconnect()


class Pinger(threading.Thread):
    """Pings ip once a second, noting the times (time.monotonic) of lost pings."""

    def __init__(self, ip):
        super().__init__(daemon=True)
        count, wait = ('-n', '-w') if sys.platform == 'win32' else ('-c', '-W')
        self.command = ['ping', count, '1', wait, '1000' if sys.platform == 'win32' else '1', ip]
        self.lost = []
        self.sent = 0
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            started = time.monotonic()
            # Windows ping exits 0 for "Destination host unreachable", so look for a reply time too
            # ("time=12ms" or "time<1ms"; "Request timed out" doesn't match).
            done = subprocess.run(self.command, capture_output=True, text=True)
            self.sent += 1
            if done.returncode != 0 or not re.search(r'time[=<]', done.stdout):
                self.lost.append(started)
            self.stopping.wait(max(0, 1 - (time.monotonic() - started)))

    def lost_between(self, start, end, margin=2):
        return any(start - margin <= t <= end + margin for t in self.lost)


def soak(dev, seconds, max_latency, ip=None):
    """Polls /api/info every 5 s. With ip, pings alongside and lets network blips pass."""
    pinger = Pinger(ip) if ip else None
    if pinger:
        pinger.start()
    bad, latencies = [], []  # bad: (start, end, stamp, description)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        stamp = time.strftime('%H:%M:%S')
        start = time.monotonic()
        try:
            status, _, _, elapsed = dev.request('GET', '/api/info')
            latencies.append(elapsed)
            if status != 200:
                bad.append((start, time.monotonic(), stamp, f'HTTP {status}'))
            elif elapsed > max_latency:
                bad.append((start, time.monotonic(), stamp, f'{elapsed * 1000:.0f} ms'))
        except OSError as e:
            bad.append((start, time.monotonic(), stamp, repr(e)))
        time.sleep(5)
    if pinger:
        pinger.stopping.set()
        pinger.join(5)
        time.sleep(2.5)  # so a blip at the very end can still be matched
    stalls = 0
    for start, end, stamp, description in bad:
        if pinger and pinger.lost_between(start, end):
            print(f'SOAK_NETWORK {stamp} {description} (pings lost too: Wi-Fi or access point)', flush=True)
        else:
            stalls += 1
            print(f'SOAK_STALL {stamp} {description}', flush=True)
    worst = max(latencies) if latencies else 0
    detail = f'{len(latencies)} polls, {stalls} stalls, {len(bad) - stalls} network blips, worst {worst * 1000:.0f} ms'
    if pinger:
        detail += f', {len(pinger.lost)}/{pinger.sent} pings lost'
    check('soak', stalls == 0, detail)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', required=True, help='Device hostname or IP')
    parser.add_argument('--timeout', type=float, default=5, help='HTTP timeout in seconds (default 5)')
    parser.add_argument('--skip-mqtt', action='store_true')
    parser.add_argument('--broker', help="Override the broker address from the device's config")
    parser.add_argument('--mqtt-port', type=int, default=1883)
    parser.add_argument('--mqtt-user', help="MQTT user for the test's own login (default: the device's user)")
    parser.add_argument('--mqtt-password',
                        help='MQTT password (default: local config.json, PICOPIR_MQTT_PASSWORD or PLASMASTICK_MQTT_PASSWORD)')
    parser.add_argument('--password', default=os.environ.get('PICOPIR_WEB_PASSWORD'),
                        help='Web password, if one is set (default: PICOPIR_WEB_PASSWORD)')
    parser.add_argument('--wave', action='store_true',
                        help='Wait for someone to wave at the sensor, and check the motion and the clear over MQTT')
    parser.add_argument('--soak', type=float, default=0, help='Poll /api/info for this many seconds afterwards')
    parser.add_argument('--max-latency', type=float, default=2.0, help='Soak latency limit in seconds (default 2)')
    parser.add_argument('--no-ping', action='store_true',
                        help="Don't ping during the soak: every failed or slow poll then fails it")
    args = parser.parse_args()

    try:
        ip = socket.gethostbyname(args.host)
    except OSError as e:
        print(f'UNREACHABLE cannot resolve {args.host}: {e}')
        return 2
    report('PASS', 'resolve', f'{args.host} -> {ip}')

    # By the IPv4 address resolved above: a .local name left to the HTTP client can come
    # back from Windows' mDNS as an IPv6 address it can't connect to (flowinfo out of range).
    dev = Device(ip, args.timeout, args.password)
    try:
        dev.info()
        original = dev.sensor()
    except (OSError, RuntimeError) as e:
        print(f'UNREACHABLE {args.host}: {e}')
        return 2

    try:
        config = test_http(dev)
        if args.skip_mqtt:
            report('SKIP', 'mqtt', '--skip-mqtt')
        else:
            test_mqtt(dev, config, args)
    except (OSError, RuntimeError) as e:
        report('FAIL', 'aborted', f'device stopped responding: {e}')
    finally:
        try:
            dev.request('POST', '/api/config', {'sensor': original})
            print(f'RESTORED sensor={dev.sensor()}')
        except (OSError, RuntimeError) as e:
            print(f'RESTORE_FAILED {e}')

    if args.soak:
        soak(dev, args.soak, args.max_latency, None if args.no_ping else ip)

    failed = results.count('FAIL')
    print(f'RESULT {"FAIL" if failed else "PASS"} passed={results.count("PASS")} failed={failed} '
          f'skipped={results.count("SKIP")}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
