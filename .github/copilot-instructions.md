# Project Guidelines

## Purpose
Pico PIR is MicroPython firmware for a Raspberry Pi Pico W with a PIR motion sensor (an HC-SR501 or similar).
It reports motion over MQTT, with Home Assistant discovery, and has a web page for its status and settings.

It is built on [App Base](https://github.com/EarthstormSoftware/app-base), the code the Earthstorm devices share,
kept as a copy in `src/base` (from App Base's `update-base.ps1 -Product src`, recorded in `src/base/VERSION`). Never
edit the copy: `tests/test_base_copy.py` fails if it's changed. A fix to shared code goes into App Base, then a new copy
is made here. PlasmaStick Controller moves onto the same base next.

## Code Style
- Target runtime is MicroPython on Raspberry Pi Pico W; prefer MicroPython modules and APIs (`uasyncio`, `ujson`, `network`, `machine`) over CPython equivalents, through `base.compat` where the base already wraps them.
- Keep long-running workflows asynchronous and non-blocking. Controller loops should always yield (for example `await sleep_ms(...)`).
- Follow existing logging pattern: module-level `TAG` plus `Log.info/warn/error/debug(...)` (`base.log`).
- Preserve existing module boundaries and naming conventions.

## Architecture
- Layout: device code is in `src/` (deployed to the device root), App Base in `src/base/` (deployed to `/base`), `mqtt_as` in `src/lib/` (deployed to `/lib`), host-side tooling in `scripts/`, host-side tests in `tests/`.
- Entry point is `main.py`, a stub: it runs `otaboot` (applies or rolls back a Wi-Fi update) then imports `app.py`, which runs `product.py` on App Base (`base.device.run`). `main.py` and `otaboot` (App Base's `base/pico/otaboot.py`, deployed to the root as `otaboot.mpy`) are USB-only (an update over Wi-Fi refuses them) and must stay self-contained; this firmware never runs `boot.py`.
- `product.py` is Pico PIR as an App Base product: its names (setup hotspot, name prefix, Home Assistant model), its settings (`device.pir_pin`, `sensor.*`), its `/api/info` fields, the state topic, the set topic's commands and its Home Assistant entities. `motion.py` is the sensor (`MotionTracker` is the timing, tested off the device).
- App Base does the rest: the web server and API (`base.web`, with a Pico's own routes in `base/pico/web.py`), settings (`base.settings`, kept in 1.0's `config.json` layout by `base.pico.Codec`), MQTT and Home Assistant discovery (`base.mqtt`, with `mqtt_as` through `base/pico/mqtt.py`), and on a Pico Wi-Fi with its fallback, setup mode, Wi-Fi updates, NTP and local time, the watchdog, BOOTSEL and the onboard LED (`base/pico`).
- `www/index.html`: the web UI, a static page on App Base's `/base.css` and `/base.js`, streamed from flash, that loads its values from `/api/info` and `/api/config` (keep it that way rather than building HTML in Python, to save heap).
- The topics and Home Assistant entities must stay 1.0's (`tests/ha_v1.json`), so Home Assistant keeps the device's history.
- Configuration source of truth is `config.json` on the device. The repo only holds `config.example.json`; a local `config.json` is gitignored.
- Roadmap: `docs/roadmap.md`.

## Build And Test
- There is no local build system. `tests/` has host-side pytest tests for pure logic (`py -m pytest`; `tests/conftest.py` stubs `machine`, `network`, `rp2` and the u-modules). Add tests there for new parsing/validation logic. Hardware and network behaviour is still verified on the device.
- Deploy with `scripts/deploy.ps1` (uses `mpremote`; compiles everything except `main.py`, including `src/lib/`, to `.mpy`). It keeps the device's `config.json` unless `-ResetConfig` is passed. `-WiFi <host>` updates over Wi-Fi instead (needs the device's web password).
- `scripts/device_log.py --reset --until-ready` captures the boot log and checks its milestones.
- `scripts/smoke_test.py --host <device> --mqtt-user <test user>` checks a running device over HTTP and MQTT (`--wave` adds real motion); the `smoke-test` skill runs deploy, boot log and smoke test together.
- `scripts/new_device.py` sets up a new board (firmware, Wi-Fi, its own MQTT user from Mosquitto Manager), from the gitignored `device-template.json`.
- For mqtt_as upgrades and rollback/testing commands, follow `docs/mqtt_as_maintenance.md`.
- Minimum validation after behavior changes:
  - Device boots and main tasks start.
  - WLAN connects/reconnects correctly.
  - MQTT connects and the retained `status` and `info` topics are published.
  - No blocking loop/starvation regressions during extended runtime.

## Repository Rules
- Never commit personal setup details: Wi-Fi names, passwords, broker addresses, MQTT users, serial numbers.
- Commit messages use Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`).

## Conventions
- Treat WLAN and MQTT as coordinated but separate lifecycles: WLAN state drives MQTT connect/reconnect timing.
- Avoid introducing blocking calls in any task path that can run inside controller loops.
- Keep config changes backward-compatible where possible; existing deployments depend on current `config.json` structure.
