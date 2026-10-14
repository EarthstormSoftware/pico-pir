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

## 4. Home Assistant

One device, with:
- a motion `binary_sensor` (`device_class: motion`);
- the last motion as a timestamp sensor;
- the clear time as a `number`, and sensing on/off as a `switch`;
- the health sensors (already there), and availability from the `status` topic.

The room is left unset: Home Assistant asks for one, and it can be set there later.

## 5. Finish

- `new_device.py`, as in PlasmaStick: firmware, Wi-Fi and the board's own MQTT user (from Mosquitto Manager) in one go.
- A smoke-test script and skill.
- README and this roadmap.
