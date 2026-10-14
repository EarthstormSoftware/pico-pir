import pytest

from motion import CLEAR, MOTION, SETTLE_S, MotionTracker

SETTLED = SETTLE_S * 1000


@pytest.fixture
def tracker():
    return MotionTracker(0)


def feed(tracker, readings, enabled=True, clear_after_s=30):
    """readings: (ms, level) pairs. Returns the events, as (ms, event)."""
    events = []
    for ms, level in readings:
        event = tracker.update(level, ms, enabled, clear_after_s)
        if event:
            events.append((ms, event))
    return events


def test_motion_is_ignored_while_the_sensor_settles(tracker):
    assert feed(tracker, [(0, 1), (SETTLED - 1, 1)]) == []
    assert tracker.settling and tracker.count == 0
    assert feed(tracker, [(SETTLED, 1)]) == [(SETTLED, MOTION)]
    assert not tracker.settling


def test_motion_straight_away_then_clear_once_nothing_is_seen_for_that_long(tracker):
    t = SETTLED
    events = feed(tracker, [(t, 1), (t + 3000, 1), (t + 3050, 0), (t + 32999, 0), (t + 33000, 0)])
    assert events == [(t, MOTION), (t + 33000, CLEAR)]  # 30 s after the last high reading
    assert not tracker.active


def test_motion_again_before_the_clear_time_keeps_it_going(tracker):
    t = SETTLED
    events = feed(tracker, [(t, 1), (t + 3000, 0), (t + 20000, 1), (t + 23000, 0), (t + 40000, 0),
                            (t + 53000, 0)])
    assert events == [(t, MOTION), (t + 53000, CLEAR)]
    assert tracker.count == 1


def test_each_detection_is_counted(tracker):
    t = SETTLED
    feed(tracker, [(t, 1), (t + 3000, 0), (t + 8000, 0), (t + 9000, 1), (t + 12000, 0), (t + 17000, 0)],
         clear_after_s=5)
    assert tracker.count == 2


def test_turning_sensing_off_clears_and_ignores_the_sensor(tracker):
    t = SETTLED
    assert feed(tracker, [(t, 1)]) == [(t, MOTION)]
    assert feed(tracker, [(t + 100, 1), (t + 200, 1)], enabled=False) == [(t + 100, CLEAR)]
    assert not tracker.active and tracker.count == 1
    assert feed(tracker, [(t + 300, 1)]) == [(t + 300, MOTION)]


def test_a_shorter_clear_time_applies_at_once(tracker):
    t = SETTLED
    feed(tracker, [(t, 1), (t + 1000, 0)])
    assert feed(tracker, [(t + 6000, 0)], clear_after_s=5) == [(t + 6000, CLEAR)]
