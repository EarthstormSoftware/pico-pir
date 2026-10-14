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


class Config:
    pir_pin = 22

    class sensorconfig:
        enabled = True
        clear_after_s = 30


@pytest.fixture
def sensor():
    from motion import MotionSensor
    s = MotionSensor(Config())
    s.tracker = MotionTracker(0)
    return s


def detect(sensor, start_ms, high_ms, wall):
    """A detection: the output high from start_ms for high_ms, then low until it clears.
    wall: time.time() at start_ms."""
    end = start_ms + high_ms + 30000
    for ms in range(start_ms, end + 1, 50):
        sensor.poll(1 if ms < start_ms + high_ms else 0, ms, wall + (ms - start_ms) // 1000)
    return end


def test_detections_are_kept_newest_first_with_their_start_and_end(sensor):
    end = detect(sensor, SETTLED, 3000, 1000)
    detect(sensor, end + 10000, 3000, 2000)
    info = sensor.info()
    assert info['recent'] == [[2000, 2032], [1000, 1032]]
    assert info['last'] == 2000 and info['count'] == 2 and not info['active']


def test_a_detection_still_going_has_no_end(sensor):
    sensor.poll(1, SETTLED, 5000)
    assert sensor.info()['recent'] == [[5000, None]]
    assert sensor.info()['active']


def test_only_the_latest_detections_are_kept(sensor):
    from motion import RECENT
    ms = SETTLED
    for n in range(RECENT + 3):
        ms = detect(sensor, ms, 1000, n * 100) + 1000
    recent = sensor.info()['recent']
    assert len(recent) == RECENT
    assert recent[0][0] == (RECENT + 2) * 100
    assert sensor.info()['count'] == RECENT + 3


def test_detection_times_move_with_the_clock(sensor):
    end = detect(sensor, SETTLED, 3000, 60)
    sensor.poll(1, end + 1000, 200)
    sensor.shift_clock(1000)
    assert sensor.info()['recent'] == [[1200, None], [1060, 1092]]


def test_no_detections_yet(sensor):
    assert sensor.info()['last'] is None and sensor.info()['recent'] == []
