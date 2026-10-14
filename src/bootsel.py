"""The Pico W's BOOTSEL button, as the way back into setup mode.

The board has no other button. Held while plugging in, BOOTSEL starts the USB
bootloader (for flashing MicroPython), so it can't be read at power-on the way
PlasmaStick reads its button. Instead, holding it for HOLD_MS while the firmware
runs restarts into setup mode, keeping the saved settings.
"""
import time
import uasyncio as asyncio
import rp2
from log import Log

TAG = "BTN"
HOLD_MS = 5000
POLL_MS = 100


class BootselButton:

    def __init__(self, pico_led):
        self.pico_led = pico_led
        self.pressed_at = None
        self._led_before = None  # what the LED was doing, put back on release

    def update(self, pressed, now):
        """Track the button; True once it has been held for HOLD_MS."""
        led = self.pico_led
        if not pressed:
            if self.pressed_at is not None:
                Log.info(TAG, 'BOOTSEL released')
                led.set_state(*self._led_before)
            self.pressed_at = None
            return False
        if self.pressed_at is None:
            self.pressed_at = now
            Log.info(TAG, f'BOOTSEL pressed: hold for {HOLD_MS // 1000} s to start setup mode')
            self._led_before = (led.current_state, led.flash, led.flash_interval)
            led.set_state(True, True, 100)  # fast flashing while held
        return time.ticks_diff(now, self.pressed_at) >= HOLD_MS

    async def controller(self):
        while True:
            if self.update(rp2.bootsel_button() == 1, time.ticks_ms()):
                Log.warn(TAG, 'BOOTSEL held: restarting into setup mode')
                from deviceconfig import DeviceConfig
                DeviceConfig.request_setup_mode()
                import watchdog
                watchdog.disable()  # setup mode runs without one
                import machine
                machine.soft_reset()
            await asyncio.sleep_ms(POLL_MS)
