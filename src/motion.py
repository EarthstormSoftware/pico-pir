"""The PIR motion sensor: motion straight away, clear once nothing has moved for a while.

The sensor's output (an HC-SR501 or similar) goes high when it sees movement
and drops a few seconds after the movement stops. Motion is reported as soon as
it goes high, and clear once it has stayed low for sensor.clear_after_s. Set the
HC-SR501's time knob fully anticlockwise (about 3 s) and its jumper to repeat
(H), so the firmware decides the timing. After its output drops it ignores
movement for about 3 s, so gaps shorter than that can't be seen.

A PIR sensor's output is unreliable for a minute or so after power-on, so it is
ignored for SETTLE_S. MotionTracker is the timing, kept free of hardware so it
can be tested off the device; MotionSensor reads the pin.
"""
import time
import uasyncio as asyncio
from log import Log

TAG = "PIR"

SETTLE_S = 60
POLL_MS = 50

MOTION = 'motion'
CLEAR = 'clear'


class MotionTracker:
    """Turns readings of the sensor's output into motion and clear events."""

    def __init__(self, now_ms, settle_s=SETTLE_S):
        self._settled_at = time.ticks_add(now_ms, settle_s * 1000)
        self.settling = True
        self.active = False
        self.count = 0  # detections since boot
        self._last_high = None

    def update(self, level, now_ms, enabled, clear_after_s):
        """level: the sensor's output now. Returns MOTION, CLEAR or None."""
        if self.settling:
            if time.ticks_diff(now_ms, self._settled_at) < 0:
                return None
            self.settling = False
        if not enabled:
            return self._clear()
        if level:
            self._last_high = now_ms
            if not self.active:
                self.active = True
                self.count += 1
                return MOTION
            return None
        if self.active and time.ticks_diff(now_ms, self._last_high) >= clear_after_s * 1000:
            return self._clear()
        return None

    def _clear(self):
        if not self.active:
            return None
        self.active = False
        return CLEAR


class MotionSensor:

    def __init__(self, device_config):
        from machine import Pin
        self.device_config = device_config  # its sensorconfig is read on every poll, so saved settings apply at once
        self.pin = Pin(device_config.pir_pin, Pin.IN, Pin.PULL_DOWN)  # low if nothing is connected
        self.tracker = MotionTracker(time.ticks_ms())
        self.last_motion = None  # time.time() when motion was last detected
        self._level = 0
        self._level_since = time.ticks_ms()
        Log.info(TAG, f'PIR sensor on GP{device_config.pir_pin}; ignoring it for {SETTLE_S} s while it settles')

    def shift_clock(self, seconds):
        """The clock was just moved by seconds: keep the last motion time right."""
        if self.last_motion is not None:
            self.last_motion += seconds

    def info(self):
        t = self.tracker
        return {'enabled': self.device_config.sensorconfig.enabled, 'settling': t.settling, 'active': t.active,
                'count': t.count, 'last': self.last_motion}

    async def controller(self):
        while True:
            cfg = self.device_config.sensorconfig
            was_settling = self.tracker.settling
            level = self.pin.value()
            now = time.ticks_ms()
            if level != self._level:
                # The sensor's own timing (its time knob), for checking how it is set up.
                Log.debug(TAG, f'Output {"high" if level else "low"} (was {"high" if self._level else "low"} for '
                               f'{time.ticks_diff(now, self._level_since) / 1000:.1f} s)')
                self._level, self._level_since = level, now
            event = self.tracker.update(level, now, cfg.enabled, cfg.clear_after_s)
            if was_settling and not self.tracker.settling:
                Log.info(TAG, 'PIR sensor settled')
            if event == MOTION:
                self.last_motion = time.time()
                Log.info(TAG, f'Motion detected ({self.tracker.count} since boot)')
            elif event == CLEAR:
                Log.info(TAG, 'Motion cleared')
            await asyncio.sleep_ms(POLL_MS)
