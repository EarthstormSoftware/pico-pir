import pytest

from bootsel import HOLD_MS, BootselButton


class FakeLed:
    def __init__(self):
        self.current_state, self.flash, self.flash_interval = True, False, 0  # on: Wi-Fi connected

    def set_state(self, state, flash=False, interval=0):
        self.current_state, self.flash, self.flash_interval = state, flash, interval


@pytest.fixture
def led():
    return FakeLed()


def test_a_hold_asks_for_setup_mode_only_after_hold_ms(led):
    button = BootselButton(led)
    assert not button.update(True, 1000)
    assert led.flash  # shows the press is seen
    assert not button.update(True, 1000 + HOLD_MS - 1)
    assert button.update(True, 1000 + HOLD_MS)


def test_letting_go_starts_again_and_restores_the_led(led):
    button = BootselButton(led)
    button.update(True, 0)
    assert not button.update(False, HOLD_MS - 100)
    assert (led.current_state, led.flash) == (True, False)
    assert not button.update(True, HOLD_MS)
    assert not button.update(True, 2 * HOLD_MS - 1)


def test_not_pressed_does_nothing(led):
    button = BootselButton(led)
    assert not button.update(False, 0)
    assert not button.update(False, HOLD_MS * 10)
    assert (led.current_state, led.flash) == (True, False)
