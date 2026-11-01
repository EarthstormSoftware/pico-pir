"""
The platform for a Juggle OS app: what the OS does that a Pico does itself.

The app's state (config.json and anything else it writes) is in JUGGLE_STATE (/data/app-state), as the rest of the Pi
is read-only. The device name is the Pi's: it's shown, and changed in the OS's setup mode (SSH and juggle know the Pi
by it). A restart ends the app, which systemd starts again.

What the OS does for the app, it asks for through app-request (the app user may run it as root by sudo): a new
version of itself, on trial; the time zone and time server; restarting the Pi; setup mode (to rename the Pi, or change
its Wi-Fi), a factory reset, and the Wi-Fi fallback (setup mode by itself when Wi-Fi doesn't connect: on or off, after
how long, for how long); a new Wi-Fi network. Juggle OS's app contract (docs/app-contract.md there) has them all from
Juggle 1.0, and the OS says which contract it gives in JUGGLE (juggle()): the version, never the presence of a file,
says what's there. On a PC, or an OS before 1.0, there's no JUGGLE, so none of it is offered. systemd's watchdog
restarts the app if its event loop stops (watchdog()).
"""
import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path

NAME_EDITABLE = False
STATE = os.environ.get('JUGGLE_STATE', str(Path.cwd() / 'state'))
# 80 on the Pi; PORT for trying it on a PC where 80 is taken
PORT = int(os.environ.get('PORT', 80))
_STARTED = time.monotonic()


def state_path(name):
    os.makedirs(STATE, exist_ok=True)
    return os.path.join(STATE, name)


def device_name():
    return socket.gethostname()


def _read(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip()
    except OSError:
        return None


def health():
    """The Pi's health for /api/info. os's version, build and slot are read from files outside the app contract, so
    each may be null."""
    temperature = _read('/sys/class/thermal/thermal_zone0/temp')
    uptime = _read('/proc/uptime')
    release = dict(line.split('=', 1) for line in (_read('/etc/juggle-release') or '').splitlines() if '=' in line)
    try:
        slot = subprocess.run(['/usr/lib/juggle/slot'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        slot = None
    now, _ = local_time()
    return {
        # Local time, as a Pico gives it: YYYY-MM-DDTHH:MM:SS, or None until the clock is right
        'time': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime()) if now is not None else None,
        'time_zone': time_zone(),
        'app_uptime_s': int(time.monotonic() - _STARTED),
        'uptime_s': int(float(uptime.split()[0])) if uptime else None,
        'temperature_c': round(int(temperature) / 1000, 1) if temperature else None,
        'load': round(os.getloadavg()[0], 2) if hasattr(os, 'getloadavg') else None,
        'os': {'version': release.get('VERSION'), 'build': release.get('BUILD_STAMP'), 'slot': slot or None,
               'requests': os_requests(), 'setup': os_setup()},
        'update': update_result(),
    }


# systemd-timesyncd makes this once the clock is right (a Pi has no clock of its own while it's off)
SYNCHRONIZED = '/run/systemd/timesync/synchronized'


def local_time():
    """(local seconds since 1970, local time minus UTC in seconds), or (None, None) until the clock is right. Off a
    Pi (no timesyncd), the clock is taken as right."""
    if os.path.isdir('/run/systemd') and not os.path.exists(SYNCHRONIZED):
        return None, None
    now = time.time()
    offset = time.localtime(now).tm_gmtoff
    return int(now) + offset, offset


def time_zone():
    """The Pi's time zone's name (set when the card is flashed), such as Europe/London."""
    try:
        target = os.path.realpath('/etc/localtime')
    except OSError:
        return None
    return target.split('zoneinfo/', 1)[1] if 'zoneinfo/' in target else None


APP_REQUEST = '/usr/lib/juggle/app-request'
# How long the event loop may stop before systemd restarts the app, and how often it says it's alive
WATCHDOG_S = 30
WATCHDOG_EVERY_S = 10


class RequestError(Exception):
    pass


def juggle():
    """The Juggle OS version the OS gives the app (JUGGLE, "MAJOR.MINOR", from the app contract for v1.0) as (major,
    minor), or None on an OS from before it, or on a PC."""
    major, _, minor = os.environ.get('JUGGLE', '').partition('.')
    return (int(major), int(minor)) if major.isdigit() and minor.isdigit() else None


def _since(level):
    """Whether the OS has what came in Juggle level (major, minor): a later minor of the same major has it too."""
    have = juggle()
    return have is not None and have[0] == level[0] and have >= level


ONLY = 'Only on Juggle OS 1.0 or later.'


def os_requests():
    """Whether the OS takes the app's requests: the update, the clock, restarting the Pi."""
    return _since((1, 0))


def _request(*args, given=None):
    """Asks the OS (app-request), with given on its stdin (a password, kept out of the process list); its answer, or
    RequestError with why not."""
    if not os_requests():
        raise RequestError(ONLY)
    command = [APP_REQUEST, *args] if os.geteuid() == 0 else ['sudo', '-n', APP_REQUEST, *args]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60, input=given)
    except (OSError, subprocess.SubprocessError) as e:
        raise RequestError(str(e))
    if done.returncode != 0:
        raise RequestError((done.stderr or done.stdout).strip() or 'app-request failed')
    return done.stdout.strip()


def clock_settings():
    """{"time_zone", "ntp"} as the OS has them."""
    return json.loads(_request('settings'))


def set_time_zone(name):
    _request('time-zone', name)
    time.tzset()  # this process's local time follows at once


def set_ntp(server):
    _request('ntp', server)


def time_zones():
    """The time zones the OS knows, by name."""
    import zoneinfo
    return sorted(zone for zone in zoneinfo.available_timezones() if '/' in zone and not zone.startswith(('Etc/', 'SystemV/')))


def update(data):
    """Installs data (a .tar of the app's folder) as a new version, on trial: the OS restarts the app with it, and
    puts this one back if it doesn't keep running. update_result() says how it went."""
    path = state_path('update.tar')
    temporary = path + '.new'
    with open(temporary, 'wb') as f:
        f.write(data)
    os.replace(temporary, path)
    _request('update')


def update_result():
    """How the last update went: {"version", "success", "message", "time"}, or None."""
    text = _read(state_path('update-result.json'))
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def restart_device():
    """Restarts the Pi."""
    _request('restart')


def os_setup():
    """Whether the OS has setup mode for the app: restarting into it, a factory reset, the Wi-Fi fallback."""
    return _since((1, 0))


def os_wifi():
    """Whether the OS takes a new Wi-Fi network from the app, and lists the ones in range."""
    return _since((1, 0))


def wifi_ssid():
    """The Wi-Fi network the Pi is set to (None on Ethernet)."""
    return clock_settings().get('wifi_ssid')


def wifi_networks():
    """The networks in range, strongest first."""
    if not os_wifi():
        raise RequestError(ONLY)
    return json.loads(_request('networks'))


def change_wifi(ssid, password):
    """A new Wi-Fi network, from the Pi's next start, on trial: the OS goes back to the network before if it doesn't
    connect within 5 minutes."""
    if not os_wifi():
        raise RequestError(ONLY)
    _request('wifi', ssid, given=password + '\n')


def _need_setup():
    if not os_setup():
        raise RequestError(ONLY)


def wifi_fallback():
    """{"enabled", "after_minutes", "for_minutes"}: whether the Pi goes into setup mode by itself when Wi-Fi doesn't
    connect, after how long, and for how long."""
    _need_setup()
    return clock_settings()['wifi_fallback']


def set_wifi_fallback(enabled, after_minutes, for_minutes):
    """Used from the Pi's next start. The OS checks the limits (1-1440 and 5-120 minutes)."""
    _need_setup()
    _request('wifi-fallback', 'on' if enabled else 'off', str(int(after_minutes)), str(int(for_minutes)))


def setup_mode():
    """Restarts the Pi into setup mode, keeping its settings until new ones are saved there."""
    _need_setup()
    _request('setup')


def factory_reset():
    """Forgets the Pi's settings (name, Wi-Fi, SSH keys, time zone) and restarts it into setup mode, as a new Pi. The
    app and its state stay (the app's own reset is separate)."""
    _need_setup()
    _request('factory-reset')


async def watchdog():
    """Asks systemd for its watchdog, then says the event loop is alive every few seconds: if it stops (a hang), systemd
    restarts the app. Only under systemd (NOTIFY_SOCKET)."""
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setblocking(False)

    def tell(message):
        try:
            sock.sendto(message, address)
        except OSError:
            pass
    tell(b'WATCHDOG_USEC=%d' % (WATCHDOG_S * 1000000))
    while True:
        tell(b'WATCHDOG=1')
        await asyncio.sleep(WATCHDOG_EVERY_S)


def routes(app, device):
    """What the app asks of the OS, as routes (base.piweb)."""
    from base import piweb
    piweb.routes(app, device)


def mqtt_client(**options):
    from base.mqttclient import Client
    return Client(**options)


def restart():
    """Ends the app; systemd starts it again (Restart=always)."""
    os._exit(0)
