---
name: smoke-test
description: Deploy the firmware to the connected Pico PIR and smoke-test it on the device - boot log, WLAN, hostname, HTTP API (motion, sensor settings, web password), the retained status/info/state topics, MQTT set commands, Home Assistant discovery, optionally a wave at the sensor and an extended soak. Use after changing anything in src/, or when asked to test, verify or smoke-test the device.
argument-hint: "[--no-deploy] [--wave] [--soak <seconds>] [--host <name-or-ip>]"
---

# Device smoke test

Runs the minimum validation from `.github/copilot-instructions.md` against the real board. Run every command from
the project root, with the PowerShell tool.

Arguments: `$ARGUMENTS`
- `--no-deploy`: test the firmware already on the board (skip step 2).
- `--wave`: also check real motion (step 5): someone has to wave at the sensor when asked.
- `--soak <seconds>`: also run the extended-runtime check (step 6). Without it, skip that step.
- `--host <name-or-ip>`: use this address instead of the hostname from the boot log.

## 1. Preflight

- `py -c "import serial, paho.mqtt"` must succeed. If not: `py -m pip install --user -r requirements-dev.txt`.
- The serial port must be free. If a step reports it in use (`SERIAL_ERROR` or `failed to access COMx`), something
  else has it open, usually an `mpremote repl` session or an `mpremote` left behind by an interrupted command.
  Stop and ask the user to close it, then carry on.

## 2. Deploy (unless `--no-deploy`)

```powershell
.\scripts\deploy.ps1 -SkipReset
```

It must print `DEPLOY_SUCCESS`. If not, stop and report the error. `-SkipReset` leaves the board stopped, so that
step 3 starts it and captures the whole boot.

## 3. Boot and connectivity (serial log)

```powershell
py scripts/device_log.py --reset --until-ready --seconds 90 --quiet
```

This saves the full log to `build/device.log` and ends with `SUMMARY`, `MILESTONE`, `HEAP` and `PROBLEM` lines.
- `wlan` and `http` must be `ok`. `mqtt` must be `ok`, unless `mqtt_disabled` is `ok`, in which case MQTT is
  reported as skipped, not failed.
- `boot` must be `ok`, unless the log has the line `--- stopped while starting Wi-Fi: hard reset ...`: the board
  was caught starting Wi-Fi and was hard-reset (a soft reset then wedges the Wi-Fi chip), so the boot's first
  lines were missed. Note it; it's not a failure.
- `provisioning ok` / `setup_ap ok` means the board started setup mode (no Wi-Fi credentials, or BOOTSEL was held
  for 5 s). Stop and tell the user. The remaining steps can't run.
- Any `PROBLEM traceback`, `error` or `memory` line is a failure. Read `build/device.log` around it and include the
  relevant lines in the report. `warning` lines are worth listing, but are not failures on their own.
- `HEAP` gives free heap after boot (about 80-90 KB is normal). Note it for comparison.
- Note the `hostname` and `wlan` (IP) values for step 4.

## 4. Resolve the device

Unless `--host` was given, try these in order, and use the first that resolves
(`py -c "import socket,sys; print(socket.gethostbyname(sys.argv[1]))" <name>`):
1. `<hostname>.local` (mDNS)
2. `<hostname>` (router DNS)
3. the IP from the `wlan` milestone

Always report which one worked. If the hostname didn't resolve, say so: that's a finding in its own right, since
the device is meant to be reachable by its name.

## 5. HTTP and MQTT

```powershell
py scripts/smoke_test.py --host <address> --mqtt-user <test user>
```

Add `--wave` if it was asked for. Before starting, tell the user to wave at the sensor once they see your next
message, then to keep out of its view until motion clears. Run the test in the background and end your turn, so
that message reaches them. The test asks for the wave a few seconds in (`WAVE at the sensor any time from now`),
keeps every state message from then on, and at the end waits up to 120 s for the motion and the clear, so the
wave's timing doesn't matter. If the sensor is still settling after power-on (its first minute), it prints `WAIT`:
a wave before then doesn't count.

It prints `PASS`/`FAIL`/`SKIP` lines and a final `RESULT` line, and puts the sensor's settings back at the end
(`RESTORED`). What it covers:
- HTTP: `/api/info` (with `motion`), the web UI, `/api/config` (and that it hides passwords), 404s, refusing a
  cross-site POST, changing the clear time and turning sensing off and on without a restart, and rejecting bad
  clear times.
- Web password: if one is set on the device, it checks that requests without it get a 401. The password itself
  comes from the `PICOPIR_WEB_PASSWORD` environment variable or `--password`. If every HTTP check fails with 401,
  ask the user for it; never print or commit it. With no password set, `http.password_required` is skipped.
- MQTT: `status` is `online`, `info` and `state` are retained and right, commands on `set` change the clear time
  and sensing (and `state` follows), bad commands are ignored, and the handler keeps working after them.
- Home Assistant discovery, when on: the configs for Motion, Last motion, Clear after, Sensing and the health
  sensors, and that the number's and the switch's own commands work. With discovery off, those are skipped.

The test needs its own MQTT login: each board has a user of its own (named after the device, made with Mosquitto
Manager) whose password only the board knows. Use the `test_mqtt_user` from `device-template.json` as
`--mqtt-user`. Its password comes from `PICOPIR_MQTT_PASSWORD`, `PLASMASTICK_MQTT_PASSWORD` (the same broker's test
account) or a local `config.json`. If MQTT fails with an authentication error, ask the user for one; never print or
commit it.

To confirm from the device's side that the commands arrived, capture the log during the test. Start
`py scripts/device_log.py --seconds 60 --quiet --out build/mqtt.log` in the background, run the smoke test, then
check `build/mqtt.log` for `Sensor settings changed over MQTT` lines and the `Ignoring command` warnings logged for
the bad commands. The `Refused cross-site POST` warning is the smoke test's own check, not a problem.

## 6. Soak (only with `--soak <seconds>`)

Run both together, the log capture in the background:

```powershell
py scripts/device_log.py --seconds <seconds+30> --quiet --out build/soak.log
py scripts/smoke_test.py --host <address> --skip-mqtt --soak <seconds>
```

Use the IP address from step 3 as `<address>` here, not the `.local` name: on Windows a `.local` lookup takes
about 3 s whenever its short cache runs out, which shows up as false `SOAK_STALL`s.

The HTTP checks run first and the settings are restored before the soak starts.

The soak pings the device every second alongside the polls. A failed or slow (over 2 s) poll is printed as
`SOAK_NETWORK` when pings were lost at the same time (a Wi-Fi or access point blip, which doesn't fail the soak),
or as `SOAK_STALL` when pings got through, which points to stalled firmware and fails it. Report network blips
as a finding, not a failure. `--no-ping` makes every failed or slow poll fail.

Also check `build/soak.log` for tracebacks, `MemoryError`, `Event loop was blocked`, `WLAN disconnected`,
`WiFi or broker is down`, `Failed to publish`, and any unexpected reboot (`Pico PIR starting` appearing again).
The heap lines are collected first, so a steady fall in `Heap free` means a real leak.

## 7. Report

Give a short table: Deploy, Boot, WLAN, Hostname (which address worked), HTTP, MQTT, Motion (with `--wave`),
Soak, each with pass, fail or skipped, plus a one-line detail. Under it, list each failure with its evidence (log
lines, or the `FAIL` line). Don't try to fix anything in this command. Report, and let the user decide.
