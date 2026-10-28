"""
The platform on a Pico W: what ApplPi OS does for an app on a Pi, a Pico does itself (Wi-Fi, setup mode, Wi-Fi
updates, the clock, the watchdog), with the onboard LED and the BOOTSEL button.

The files on the board's root, as the firmware before the base kept them, so a device carries on as it was (and the
old firmware can still read them if an update rolls back):
  config.json         the settings; the web password as a hash (web.password_hash), the MQTT password encrypted
                      (mqtt.password_iv, mqtt.password_enc)
  provisioning.json   the Wi-Fi network and the name given in setup mode, the password encrypted
  device_name.json    the name set on the page, which wins over the others
  setup_request       setup mode at the next start (BOOTSEL held, the page, the Wi-Fi fallback), used once
The product gives SETUP_SSID (the setup hotspot) and NAME_PREFIX (for the name made from the board's id).

run(product), from the product's main.py (through app.py, which main.py imports), starts it all.
"""
import time
import machine
from base.compat import asyncio, json, write_atomically, collect, mem_free, sleep_ms
from base.log import Log
from base.settings import Setting, is_bool, is_int, is_one_of, is_text

TAG = 'PICO'
NAME_EDITABLE = True
PORT = 80
# The health figures Home Assistant shows (rssi and ip are empty until Wi-Fi connects, but are always announced)
SENSORS = ('rssi', 'uptime_s', 'mem_free', 'ip')
ROOT = '/'  # tests point it elsewhere
CONFIG = 'config.json'
PROVISIONING = 'provisioning.json'
NAME = 'device_name.json'
SETUP_REQUEST = 'setup_request'
CLOCK_SYNC_S = 6 * 3600  # the board's clock drifts a few seconds a day
LOCAL_TIME_CHECK_S = 15  # so summer time starts and ends on time
HEAP_LOG_S = 300
LOOP_LAG_WARN_MS = 500

_NTP_CHARS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-'


def _ntp_error(value):
    if not isinstance(value, str) or len(value) > 64 or any(c not in _NTP_CHARS for c in value):
        return 'must be a host name or address (empty: none)'
    return None


def _utc_offset_error(value):
    if not isinstance(value, int) or isinstance(value, bool) or not -720 <= value <= 840 or value % 15:
        return 'must be -720 to 840 minutes, in steps of 15'
    return None


def _encrypt(value):
    from base.pico.crypto import encrypt_password
    return '$'.join(encrypt_password(value))


def _decrypt(kept):
    from base.pico.crypto import decrypt_password
    try:
        return decrypt_password(*kept.split('$'))
    except Exception as e:  # another board's file, or a damaged one
        Log.warn(TAG, "Couldn't decrypt the MQTT password: {!r}".format(e))
        return ''


SETTINGS = {
    'device': {
        # The name in config.json, if there's none from setup mode or the page (see device_name())
        'device_name': Setting('', is_text(32), restart=True),
        # Restart the board if the firmware stops (see wdt.py)
        'watchdog': Setting(True, is_bool, restart=True),
        # Where the clock is set from (UTC); empty: it isn't
        'ntp_server': Setting('pool.ntp.org', _ntp_error, restart=True),
        # Local time: minutes ahead of UTC, and the summer time rule (see clock.py)
        'utc_offset_min': Setting(0, _utc_offset_error),
        'dst': Setting('none', is_one_of('none', 'eu', 'us')),
    },
    'wlan': {
        # Kept for a config.json written by hand: setup mode's provisioning.json wins
        'ssid': Setting('', is_text(32), restart=True),
        'password': Setting('', is_text(64), restart=True, secret=True),
        'connect_timeout': Setting(30, is_int(5, 300), restart=True),
        'reconnect_wait': Setting(30, is_int(5, 3600), restart=True),
        'max_retries': Setting(0, is_int(0, 1000), restart=True),
        # The Wi-Fi fallback: setup mode when Wi-Fi hasn't connected since the start for so many minutes, for so many
        'fallback': Setting(True, is_bool),
        'fallback_after_min': Setting(5, is_int(1, 60)),
        'fallback_for_min': Setting(10, is_int(5, 120)),
    },
    'mqtt': {
        'enabled': Setting(True, is_bool, restart=True),
        'password': Setting('', is_text(64, allow_empty=False), restart=True, secret=True, store=_encrypt,
                            reveal=_decrypt),
        'clean': Setting(True, is_bool, restart=True),
        'queue_len': Setting(1, is_int(1, 20), restart=True),
    },
}


class Codec:
    """config.json in the old firmware's layout."""

    @staticmethod
    def load(stored):
        stored = dict(stored)
        web = dict(stored.get('web') or {})
        if web.get('password'):  # typed into the file by hand: hashed as the old firmware did
            from base.passwords import hash_password
            web['password'] = hash_password(web['password'])
        else:
            web['password'] = web.get('password_hash', '')
        stored['web'] = web
        mqtt = dict(stored.get('mqtt') or {})
        if mqtt.get('password'):  # by hand too
            mqtt['password'] = _encrypt(mqtt['password'])
        elif mqtt.get('password_iv') and mqtt.get('password_enc'):
            mqtt['password'] = mqtt['password_iv'] + '$' + mqtt['password_enc']
        stored['mqtt'] = mqtt
        return stored

    @staticmethod
    def dump(values):
        stored = dict(values)
        web = dict(values['web'])
        web['password_hash'] = web.pop('password')
        stored['web'] = web
        mqtt = dict(values['mqtt'])
        password = mqtt.pop('password')
        if password:
            mqtt['password_iv'], mqtt['password_enc'] = password.split('$')
        stored['mqtt'] = mqtt
        return stored


# What run() sets up
product = None
device = None
led = None
wlan = None
_started = time.time()
_clock_set = False
_utc_offset = 0
_stall = None
_dog = None
_name = None
_clock_listeners = []


def path(name):
    return ROOT + name


def state_path(name):
    return path(name)


def _load(name):
    try:
        with open(path(name)) as f:
            data = json.loads(f.read())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def device_name():
    """Set on the page, else given in setup mode, else in config.json, else made from the board's id."""
    global _name
    if _name is None:
        from base.pico import identity
        provisioned = _load(PROVISIONING) or {}
        configured = (_load(CONFIG) or {}).get('device') or {}
        _name = identity.clean(identity.renamed(path(NAME)) or provisioned.get('device_name') or
                               configured.get('device_name'), product.NAME_PREFIX)
    return _name


def rename(name):
    """Saves a new name (from the page), used from the next start. Returns why it couldn't, or None."""
    from base.pico import identity
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 32:
        return 'The name must be 1-32 characters.'
    cleaned = identity.clean(name, product.NAME_PREFIX)
    write_atomically(path(NAME), json.dumps({'name': cleaned}))
    Log.info(TAG, 'Renamed {}: from the next start'.format(cleaned))
    return None


def health():
    import network
    sta = network.WLAN(network.STA_IF)
    connected = sta.isconnected()
    try:
        rssi = sta.status('rssi') if connected else None
    except (OSError, ValueError):
        rssi = None
    now, _ = local_time()
    return {
        'uptime_s': time.time() - _started,
        # Local time, as a Pi gives it, or None until the clock is set (it says 2021 till then)
        'time': '{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}'.format(*time.gmtime(now)[:6]) if now is not None else None,
        'rssi': rssi,
        'ip': sta.ifconfig()[0] if connected else None,
        'ssid': wlan.ssid if wlan is not None else None,
        'watchdog_stall': _stall,
        'mem_alloc': _mem_alloc(),
        # Setup mode, the Wi-Fi fallback and a factory reset, from the page (as an ApplPi OS with setup mode)
        'setup': True,
        'setup_ssid': product.SETUP_SSID,
    }


def _mem_alloc():
    import gc
    return gc.mem_alloc()


def local_time():
    """(local seconds since 1970, local time minus UTC in seconds), or (None, None) until the clock is set."""
    if not _clock_set:
        return None, None
    return int(time.time()) + _utc_offset, _utc_offset


def on_clock_moved(listener):
    """listener(seconds) is called when NTP moves the clock, so times kept as time.time() can move with it."""
    _clock_listeners.append(listener)


def mqtt_client(**options):
    from base.pico.mqtt import Client
    s = device.settings
    return Client(clean=s.get('mqtt', 'clean'), queue_len=s.get('mqtt', 'queue_len'), **options)


def restart():
    from base.pico import wdt as watchdog
    watchdog.disable()  # the next start makes its own
    machine.soft_reset()


def setup_mode():
    """Restarts into setup mode, keeping the settings."""
    with open(path(SETUP_REQUEST), 'w') as f:
        f.write('1')
    restart()


def _remove(*names):
    import os
    for name in names:
        try:
            os.remove(path(name))
        except OSError:
            pass


def reset():
    """After the settings are reset (device.reset()): the name set on the page goes too."""
    _remove(NAME)


def factory_reset():
    """Forgets the settings, the name and the Wi-Fi network, and restarts into setup mode."""
    device.reset()
    _remove(PROVISIONING, SETUP_REQUEST)
    Log.warn(TAG, 'Factory reset: settings and Wi-Fi forgotten')
    restart()


def _save_setup(name, ssid, password):
    """What setup mode's page saves: the network and name (provisioning.json). It also clears the web password (so a
    forgotten one can be recovered by someone with the board) and the name set on the page. Returns the name kept."""
    from base.pico import identity
    from base.pico.crypto import encrypt_password
    name = identity.clean(name, product.NAME_PREFIX)
    iv, encrypted = encrypt_password(password)
    try:
        write_atomically(path(PROVISIONING), json.dumps({'device_name': name, 'ssid': ssid, 'password_iv': iv,
                                                         'password_enc': encrypted}))
        config = _load(CONFIG)
        if config and isinstance(config.get('web'), dict) and config['web'].get('password_hash'):
            config['web']['password_hash'] = ''
            write_atomically(path(CONFIG), json.dumps(config))
        _remove(NAME)
    except OSError as e:
        Log.error(TAG, "Couldn't save the settings: {}".format(e))
        return None
    return name


def _take_setup_request():
    import os
    try:
        os.remove(path(SETUP_REQUEST))
        return True
    except OSError:
        return False


def _wifi():
    """(ssid, password): from setup mode, or from a config.json written by hand; ('', '') for none."""
    from base.pico import setup
    saved = setup.load(path(PROVISIONING))
    if saved:
        return saved['ssid'], saved['password']
    wlan_config = (_load(CONFIG) or {}).get('wlan') or {}
    return wlan_config.get('ssid') or '', wlan_config.get('password') or ''


def _fallback_for_s():
    wlan_config = (_load(CONFIG) or {}).get('wlan') or {}
    minutes = wlan_config.get('fallback_for_min')
    return (minutes if isinstance(minutes, int) and 5 <= minutes <= 120 else 10) * 60


async def _clock():
    """Sets the clock (UTC) from device.ntp_server once Wi-Fi is up, then now and then; keeps local time's offset."""
    global _clock_set, _utc_offset
    from base.pico import clock
    server = device.settings.get('device', 'ntp_server')
    next_sync = 0
    while True:
        s = device.settings
        _utc_offset = clock.offset_s(time.time(), s.get('device', 'utc_offset_min'), s.get('device', 'dst'))
        Log.utc_offset = _utc_offset
        if server and time.time() >= next_sync and wlan.is_connected():
            from base.pico import ntp
            try:
                moved = ntp.set_clock(await ntp.fetch(server))
            except (OSError, ValueError) as e:
                Log.warn(TAG, "Couldn't set the clock from {}: {}".format(server, e))
                next_sync = time.time() + (600 if _clock_set else 15)  # DNS can take a moment after joining Wi-Fi
            else:
                global _started
                _started += moved  # things timed by the clock move with it
                for listener in _clock_listeners:
                    listener(moved)
                next_sync = time.time() + CLOCK_SYNC_S
                _utc_offset = clock.offset_s(time.time(), s.get('device', 'utc_offset_min'), s.get('device', 'dst'))
                Log.utc_offset = _utc_offset
                if not _clock_set:
                    Log.info(TAG, 'Clock set from {}'.format(server))
                    _clock_set = Log.clock_set = True
                    device.state_changed()  # times in the state are known now
        await asyncio.sleep(LOCAL_TIME_CHECK_S if _clock_set or not server else 1)


async def _heap():
    # Free heap now and then, collected first, so long runs show leaks or fragmentation
    while True:
        collect()
        Log.info(TAG, 'Heap free: {} bytes, allocated: {} bytes'.format(mem_free(), _mem_alloc()))
        await asyncio.sleep(HEAP_LOG_S)


async def watchdog():
    """Tells the watchdog the event loop runs, and warns when it was held up (a task that blocked stalls the page,
    MQTT and the product alike)."""
    global _dog
    if device.settings.get('device', 'watchdog'):
        from base.pico import wdt as dog
        _dog = dog.Watchdog()
        Log.info(TAG, 'Watchdog started')
    while True:
        if _dog is not None:
            _dog.heartbeat()
        started = time.ticks_ms()
        await sleep_ms(100)
        late = time.ticks_diff(time.ticks_ms(), started) - 100
        if late > LOOP_LAG_WARN_MS:
            Log.warn(TAG, 'The event loop was held up for about {} ms'.format(late))


async def start(dev):
    """Before the web server: the LED, BOOTSEL, Wi-Fi and the clock, then waits for Wi-Fi."""
    from base.pico.bootsel import Bootsel
    asyncio.create_task(led.run())
    asyncio.create_task(Bootsel(led, setup_mode).run())
    asyncio.create_task(wlan.run())
    while not wlan.is_connected():
        await asyncio.sleep(1)
    asyncio.create_task(_clock())
    asyncio.create_task(_heap())
    collect()


async def serving(server):
    """The web server has started: an update on trial is confirmed once it's still serving a moment later (the way the
    next update would arrive)."""
    await asyncio.sleep(1)
    if server.done():
        return
    import sys
    from base.pico import ota
    rolled_back = ota.take_rollback()
    if rolled_back:
        Log.warn(TAG, "The update to {} didn't start properly, so it was rolled back".format(rolled_back))
    confirmed = ota.mark_good()
    if confirmed:
        Log.info(TAG, 'Update to {} confirmed'.format(confirmed))
    del sys.modules['base.pico.ota']  # only an update needs it again
    collect()


def routes(app, dev):
    from base.pico import web
    web.routes(app, dev)


def run(product_module):
    global product, device, led, wlan, _stall
    product = product_module
    Log.info(TAG, '{} {} starting'.format(product.NAME, product.VERSION))
    from base.pico import wdt as dog
    # A watchdog left running by a soft reset would restart the board mid-start; watchdog() starts a new one
    dog.disable()
    _stall = dog.take_stall_report()
    if _stall is not None:
        Log.warn(TAG, 'Restarted by the watchdog: the firmware had stalled for {} s'.format(_stall))
    from base.pico.led import Led
    led = Led()
    ssid, password = _wifi()
    reason = 'no Wi-Fi saved' if not ssid else None
    if _take_setup_request() and not reason:
        reason = 'asked for'
    if reason:
        Log.warn(TAG, 'Setup mode: {}'.format(reason))
        from base.pico import setup
        setup.run(product, led, device_name(), _save_setup, _fallback_for_s(), bool(ssid))  # ends with a reset
    import sys
    from base.device import Device
    device = Device(product, sys.modules[__name__])
    Log.level = device.settings.get('device', 'log_level')
    Log.info(TAG, 'Name: {}'.format(device_name()))
    from base.pico.wlan import Wlan
    wlan = Wlan(ssid, password, device.settings, led, setup_mode, device_name())
    try:
        asyncio.run(device.main())
    except KeyboardInterrupt:
        # Ctrl-C (mpremote does it to deploy): to the REPL, with no restart, and an update on trial taken as good
        # (someone is at the USB port)
        if _dog is not None:
            _dog.stop()
        from base.pico import ota
        ota.mark_good()
        raise
    finally:
        asyncio.new_event_loop()
        led.set(False)
