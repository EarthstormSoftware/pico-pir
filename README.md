# Pico PIR

Pico PIR is MicroPython firmware for a Raspberry Pi Pico W with a PIR motion sensor (an HC-SR501 or similar). It
reports to an MQTT broker, with Home Assistant discovery, and has a built-in web page for its status and settings.

It is built from [PlasmaStick Controller](https://github.com/EarthstormSoftware/plasmastick-controller) and shares
its Wi-Fi setup, web server, settings, MQTT connection, clock, watchdog and Wi-Fi updates.

> **Status:** step 1 of the [roadmap](docs/roadmap.md), the base, is done: the device joins Wi-Fi, serves its web
> page and reports its health over MQTT. Reading the sensor is next.

## Features

- Motion: reported as soon as the PIR sensor sees movement, and clear once nothing has moved for
  `sensor.clear_after_s` (15 s by default, 5 s to an hour). The sensor is ignored for the first minute after
  power-on while it settles. `/api/info` has `motion`: whether there's motion now, the time of the last motion and
  how many there have been since boot. `sensor.enabled` turns sensing off. Both it and the clear time apply at
  once. The onboard LED shows Wi-Fi, as on PlasmaStick, not motion.
- Wi-Fi setup without editing files: on first boot the device opens a `PicoPIR-SETUP` access point for 10 minutes.
  Join it, then open **`http://192.168.4.1`** in a browser. Pick your network from the list (or type the name of a
  hidden one), enter its password and choose Save & Connect; the device restarts onto your network. The Wi-Fi
  password is stored encrypted with a key derived from the device.
- Getting back into setup mode (for a new router or a mistyped password): **hold the board's BOOTSEL button for
  5 seconds** while it's running (the LED flashes quickly while it's held). Don't hold it while plugging in: that
  starts the USB bootloader instead. If Wi-Fi hasn't connected after 5 attempts since boot (about 2-3 minutes), the
  device restarts into setup mode by itself. Either way it keeps its saved settings until new ones are submitted,
  and if setup mode times out it carries on with them.
- Web UI and JSON HTTP API on port 80. The Status tab shows motion now, the last motion and the last 10
  detections (when each started and cleared) beside the firmware version, uptime, time, free memory, Wi-Fi and
  MQTT, all from `/api/info`; the other tabs hold the settings, the sensor's on the Device tab. An optional web password (Network tab) protects
  both with HTTP Basic auth (any username; `curl -u any:password`). It's sent unencrypted over your network, so it
  keeps out other people on the network rather than a determined attacker. Completing setup mode removes it, if
  it's forgotten. POSTs that a browser sends from another website's page are refused either way.
- Optional MQTT, using [mqtt_as](https://github.com/peterhinch/micropython-mqtt), with optional Home Assistant
  discovery (see [MQTT](#mqtt)).
- Clock: once Wi-Fi is up, the device sets its clock from an NTP server (`device.ntp_server`, `pool.ntp.org` by
  default; blank turns it off) and again every 6 hours. Local time comes from the time zone
  (`device.utc_offset_min`, the standard offset in minutes) and summer time rule (`device.dst`: `none`, `eu` or
  `us`), on the Device tab. Until the clock is set, log times count from 2021-01-01.
- Watchdog: if the firmware stops responding (its event loop stalls for 30 s, or it hangs outright), the board's
  hardware watchdog restarts it. The next boot logs how long it had stalled, and `/api/info` reports it as
  `watchdog_stall`. Ctrl-C at the USB REPL (as `mpremote` and the deploy script do) turns it off.
  `device.watchdog` (Device tab, Diagnostics) turns it off altogether, for debugging.

## Hardware

- Raspberry Pi Pico W.
- A PIR sensor module with a digital output, wired to **GP22**. The HC-SR501 runs from 5 V (VBUS) and its output is
  3.3 V, which the Pico can read directly. Another pin can be set as `device.pir_pin` (a reset keeps it).
- On the HC-SR501, turn the time knob fully anticlockwise (about 3 s) and set the jumper to repeat (H), so the
  firmware decides how long motion lasts. After its output drops it ignores movement for about 3 s.

## Pre-requisites

- The Pico W running MicroPython 1.29 or later (the `RPI_PICO_W` build from
  [micropython.org](https://micropython.org/download/RPI_PICO_W/)). To flash it, hold BOOTSEL while plugging the
  board in (or run `py -m mpremote bootloader`), then copy the `.uf2` file to the `RPI-RP2` drive that appears.
- On the PC: Python 3 and the tools in `requirements-dev.txt`:

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

- Optional, for editor type hints: install the MicroPython stubs into `typings/` (gitignored), where Pylance finds them:

```powershell
py -m pip install --target typings micropython-rp2-rpi_pico_w-stubs
```

## Deploying to the device

Connect the Pico over USB and run:

```powershell
.\scripts\deploy.ps1
```

The script compiles everything except the small `main.py` to bytecode (`.mpy`, which saves the device compiling it
in RAM), copies it to the device (libraries into `/lib`), checks that everything arrived, and resets the
board. Keep `mpy-cross`'s bytecode version in line with the device firmware (MicroPython 1.29 loads `.mpy` v6.3),
or the device can't load the `.mpy` files.

| Option | Effect |
|---|---|
| `-Port COM4` | Use a specific serial port instead of auto-detecting |
| `-ResetConfig` | Overwrite the device's `config.json` (it is otherwise only copied when the device has none) |
| `-PreserveProvisioning:$false` | Wipe the stored Wi-Fi credentials and device name, so the device starts setup mode again |
| `-Wipe` | Delete everything on the device first, then deploy with `config.example.json` (or a local `config.json`). It boots into setup mode |
| `-SkipReset` | Don't reset the board after copying |
| `-WiFi <host>[,<host>...]` | Update over Wi-Fi instead of USB (see below) |
| `-Rebuild` | Compile every module again. Normally only modules whose source changed since the last build are compiled |

To watch it start, `py scripts\device_log.py --reset --until-ready` restarts the board, captures its boot log to
`build\device.log` and summarises it.

### Setting up a new board

`scripts/new_device.py` sets up a freshly flashed board in one go: the firmware, its Wi-Fi, an MQTT user of its own
(named after the device, created with Mosquitto Manager's `mosqman`) and its settings. First flash MicroPython for
the Pico W (see [Hardware](#hardware)); the script checks for it and won't touch a board that already has files
unless given `--force`.

Once: copy `device-template.example.json` to `device-template.json` (gitignored) and fill in your network name,
broker, `mosqman` profile, test MQTT user and the settings most boards share, then save the Wi-Fi details the
boards get:

```powershell
py scripts\new_device.py save-wifi --from-port COM5   # from a working board, over USB
py scripts\new_device.py save-wifi --prompt           # or type them
```

They're kept under `%APPDATA%\PicoPIR`, encrypted with Windows DPAPI for your Windows account only. Then for each
board:

```powershell
py scripts\new_device.py setup --port COM6 --name Hallway --clear-after 30
py scripts\new_device.py check --name Hallway --host 192.168.1.50   # after the broker has reloaded
```

`--pin` and `--clear-after` override the template for that board. Both passwords are written to the board encrypted
with its own key and are never printed. With a password-file broker, restart Mosquitto after `setup` so it loads
the new user; `check` then waits for the board to log in and runs the smoke test through the template's
`test_mqtt_user`.

### Updating over Wi-Fi

Once a device runs this firmware, later versions can go over Wi-Fi:

```powershell
$env:PICOPIR_WEB_PASSWORD = '...'   # or leave it unset to be asked
.\scripts\deploy.ps1 -WiFi stairs.local
```

It sends only the files that changed, and the device restarts to apply them. The device must have a web password
set; updates are refused without one. The new firmware is on trial until its web server is up: if it doesn't get
that far within 3 minutes, twice, the device puts the previous files back by itself and reports the rollback in its
log and at `GET /api/update`. `main.py` and `otaboot.py`, which apply updates and roll them back, are only ever
updated over USB, as are settings and credentials, which an update never touches.

## Tests

`tests/` holds host-side tests for the device code's logic (config loading and saving, the API, MQTT topics and
discovery, the captive-portal DNS replies, HTML escaping, Wi-Fi updates). They run on this PC with stand-ins for the
MicroPython-only modules, so they don't need the device:

```powershell
py -m pytest
```

Anything that touches hardware or the network still needs the device. `scripts/smoke_test.py` checks a running
board over HTTP and MQTT: the API and web page, changing the sensor's settings, the retained topics, commands on
the set topic (bad ones included) and the Home Assistant discovery configs. It puts the settings back afterwards.
`--wave` also asks you to wave at the sensor and checks the motion and the clear arrive over MQTT, and `--soak 300`
polls it for five minutes:

```powershell
py scripts\smoke_test.py --host hallway.local --mqtt-user tester --wave
```

A board with its own MQTT user is tested through another login (`--mqtt-user`), whose password comes from
`PICOPIR_MQTT_PASSWORD`. In Claude Code, the `smoke-test` skill (`.claude/skills/`) deploys, captures the boot and
runs all of this.

## Configuration

Settings live in `config.json` on the device and can be changed from the web UI. The first deploy copies
`config.example.json`. If you want your own defaults, copy it to `config.json` in the project folder (that file is
gitignored, so passwords won't be committed).

Wi-Fi credentials and the device name are set through setup mode and stored in `provisioning.json` on the device.

Saving settings only restarts the device when it has to: the device name, the Wi-Fi and MQTT settings, the time
server, the watchdog and the sensor's pin are read at boot, while the time zone, log level, web password and the
other sensor settings apply at once. The Save
button says "Save & Restart" when a change needs a restart.

The Device tab has two reset buttons. **Reset settings** puts everything back to its default (including the MQTT
settings, web password and a name set in the web UI) and keeps the Wi-Fi network. **Factory reset** also forgets
the Wi-Fi network, so the device starts setup mode. From a script: `POST /api/reset` with `{"confirm": true}`, plus
`"wifi": true` for a factory reset.

## MQTT

With MQTT enabled (MQTT tab, or `mqtt` in `config.json`), the device uses these topics under its topic base
(`mqtt.topic_base`). The base defaults to `/home/device/{name}`, where `{name}` is replaced by the device name:

| Topic | Direction | Contents |
|---|---|---|
| `<base>/status` | from the device, retained | `online` while connected. The broker sets `offline` (the MQTT will) if the device drops off |
| `<base>/info` | from the device, retained | `{"version": "0.1.0", "uptime_s": 3600, "rssi": -58, "ip": "192.168.1.20", "mem_free": 86208}`: the device's health, as in `/api/info`, on connecting and every minute |
| `<base>/state` | from the device, retained | `{"motion": true, "last": "2026-10-14T14:04:59+02:00", "count": 3, "enabled": true, "clear_after_s": 15}`: on connecting and whenever any of it changes. `last` is null until the clock is set |
| `<base>/set` | to the device | `{"enabled": false}` turns sensing off (`true` on again), `{"clear_after_s": 30}` sets the clear time (5-3600 s); either or both. Saved, as from the web page. Don't retain these: a retained command is ignored |

With **Home Assistant discovery** turned on (`mqtt.ha_discovery`), the device publishes retained discovery configs
under the `homeassistant` prefix (`mqtt.ha_prefix`), so it appears in Home Assistant as one device with:

- **Motion**, a motion sensor, and **Last motion**, a timestamp;
- **Clear after** (a number, in seconds) and **Sensing** (a switch) as its settings;
- diagnostic sensors: Wi-Fi signal, uptime, free memory and IP address.

All of them show as unavailable while the device is offline. The device has no room (area) set: give it
one in Home Assistant. Turning discovery off removes them on the next connection.

Changing the topic base, the discovery prefix or the device name restarts the device. On its next connection it
clears the retained messages it left on the old topics, so nothing stale is left on the broker or in Home
Assistant.

## Project layout

| Path | Contents |
|---|---|
| `src/` | Device code, deployed to the root of the device. `main.py` starts it: `otaboot.py` deals with any Wi-Fi update, then `app.py` runs the firmware |
| `src/www/` | The web UI, deployed to `/www`: a static page that fills itself in from the JSON API |
| `src/lib/` | Third-party libraries, deployed to `/lib`: `microdot` (web server), `mqtt_as` (MQTT client) |
| `tests/` | Host-side pytest tests (`py -m pytest`); `conftest.py` stands in for the MicroPython-only modules |
| `scripts/` | `deploy.ps1`; `wifi_update.py` (`deploy.ps1 -WiFi`); `new_device.py` (set up a new board); `device_log.py` (capture and summarise the serial log); `smoke_test.py` (test a running device over HTTP and MQTT) |
| `docs/` | The roadmap and maintenance notes |

## Known Restrictions

- Without a web password (Network tab), anyone on the same network can change the settings. With one, it's sent
  unencrypted (plain HTTP), so it keeps out other people on the network rather than a determined attacker.
- Stored passwords: the web password is kept only as a salted, repeated SHA-256 hash. The MQTT and Wi-Fi passwords
  have to be sent on, so they're encrypted (AES-256) with a key derived from the board's MAC address. That keeps
  them unreadable in a copy of `config.json` or `provisioning.json`, but not from someone with the device itself.
  The MQTT connection itself is not encrypted.
- The setup access point is open (no password) while setup mode is running.
- Phones don't reliably show a "Sign in to network" prompt for the setup access point, so open
  `http://192.168.4.1` yourself.

#### Third-party code

- [Microdot](https://github.com/miguelgrinberg/microdot), © Miguel Grinberg, MIT licence.
- [mqtt_as](https://github.com/peterhinch/micropython-mqtt), © Peter Hinch, MIT licence.
