# Roadmap

Pico PIR replaces the first, 2022 firmware on a Pico W with a PIR sensor (that version never published: its main
loop sent a variable that didn't exist). It is built from PlasmaStick Controller and keeps in step with it.

## 1. Base (done)

PlasmaStick's shared modules, adapted: Wi-Fi setup through the `PicoPIR-SETUP` hotspot, the web page and JSON API
with an optional password, settings saved to `config.json`, MQTT with the retained `status` and `info` topics,
Home Assistant discovery of the health sensors, the clock from NTP, the watchdog, Wi-Fi updates, `deploy.ps1` and
`device_log.py`. The Pico W has no button, so holding BOOTSEL for 5 s restarts into setup mode.

## 2. The sensor (done)

- Read the PIR output (GP22) without blocking; ignore the first 30-60 s after power-on while it settles.
- A "clear after" time: motion is reported straight away, and clear once nothing has moved for that long. With the
  HC-SR501, set its time knob fully anticlockwise (about 3 s) and its jumper to repeat (H), so the firmware
  decides the timing. It ignores motion for about 3 s after its output drops, so shorter gaps can't be seen.
- Last motion time, and a count of detections.
- A setting to turn sensing off.
- Tests for the timing logic, off the device.

## 3. Web page (done)

The Status tab shows motion now, the last motion and recent events, as well as the device's health. The sensor's
settings go on the Device tab.

## 4. Home Assistant (done)

One device, with:
- a motion `binary_sensor` (`device_class: motion`);
- the last motion as a timestamp sensor;
- the clear time as a `number`, and sensing on/off as a `switch`;
- the health sensors (already there), and availability from the `status` topic.

The room is left unset; it is set in Home Assistant.

## 5. Finish (done)

- `new_device.py`, as in PlasmaStick: firmware, Wi-Fi and the board's own MQTT user (from Mosquitto Manager) in one go.
- A smoke-test script and skill.
- README and this roadmap.

## 6. Onto App Base (2.0, on the device; not yet released)

The code it shares with PlasmaStick moves into [App Base](https://github.com/EarthstormSoftware/app-base), which
ApplPi apps use too, so a fix is made once (App Base's roadmap, step 2). Pico PIR keeps the sensor, its settings, its
state and commands and its page; the base does the rest, from a copy in `src/base`.

- The same topics and Home Assistant entities as 1.0 (checked against what 1.0 publishes, `tests/ha_v1.json`), and
  the same files on the board (`config.json` in 1.0's layout, `provisioning.json`, `device_name.json`), so a device
  carries on as it was, and 1.0 can still read them if the update rolls back.
- The Wi-Fi fallback becomes three settings (on or off, after how many minutes, for how many), as on ApplPi OS, and
  the Network tab can start setup mode. A factory reset is `/api/factory-reset`.
- 1.0.1 first, only so a 1.0.0 device's updater may write App Base's folders: then 2.0 goes over Wi-Fi.
- Then: the schedule (sensing on or off at set times), from App Base. (Done.)
- Checked on the device: the boot log and heap, the smoke test, Home Assistant unchanged. (Done: smoke
  test 35/35, about 62 KB free.)
- Since: Wi-Fi changed from the Network tab on trial, and a fallback of up to a day; each detection's movement time
  apart from the clear time; the web files gzipped; warnings and errors on `<base>/log`; an update's file list read a
  line at a time, so every file can go at once (`wifi_update.py --all`, tested on a device).
- PlasmaStick 2.0 is on the same base, so the two share it again.

## Later

- An LED of its own for motion, if one is wanted: the onboard LED shows Wi-Fi, as on PlasmaStick.
