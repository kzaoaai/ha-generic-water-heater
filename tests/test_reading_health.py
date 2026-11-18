"""HeatingWatch: heated time since the reading last moved (pure logic).

The heater's use of it, and what counts as "heated", is in
test_stale_reading.py.
"""

from datetime import datetime, timedelta, timezone

from custom_components.generic_water_heater.reading_health import HeatingWatch

T0 = datetime(2026, 11, 18, 10, 35, tzinfo=timezone.utc)
LIMIT = timedelta(minutes=30)


def minutes(n):
    return T0 + timedelta(minutes=n)


def watch():
    return HeatingWatch(LIMIT, 0.5)


def test_nothing_is_counted_before_a_reading():
    w = watch()
    w.heating(T0, True)
    assert w.due_in(minutes(5)) is None
    assert not w.overdue(minutes(90))


def test_heated_time_runs_to_the_limit():
    w = watch()
    w.reading(T0, 28.86)
    w.heating(T0, True)
    assert w.due_in(minutes(10)) == timedelta(minutes=20)
    assert not w.overdue(minutes(29))
    assert w.overdue(minutes(30))
    assert w.due_in(minutes(40)) == timedelta(0)


def test_time_not_heated_is_not_counted():
    w = watch()
    w.reading(T0, 28.86)
    assert w.due_in(minutes(5)) is None
    assert w.heated_for(minutes(500)) == timedelta(0)
    assert not w.overdue(minutes(500))


def test_short_runs_add_up():
    """Three minutes on, a minute off, six times: 18 minutes, not 3."""
    w = watch()
    w.reading(T0, 28.86)
    at = 0
    for _ in range(6):
        w.heating(minutes(at), True)
        w.heating(minutes(at + 3), False)
        at += 4
    assert w.heated_for(minutes(at)) == timedelta(minutes=18)
    w.heating(minutes(at), True)
    assert w.due_in(minutes(at)) == timedelta(minutes=12)


def test_repeating_the_answer_changes_nothing():
    w = watch()
    w.reading(T0, 28.86)
    w.heating(T0, True)
    w.heating(minutes(10), True)
    assert w.heated_for(minutes(20)) == timedelta(minutes=20)
    w.heating(minutes(20), False)
    w.heating(minutes(25), False)
    assert w.heated_for(minutes(30)) == timedelta(minutes=20)


def test_a_reading_that_moved_starts_the_count_again():
    w = watch()
    w.reading(T0, 28.86)
    w.heating(T0, True)
    assert w.reading(minutes(20), 29.4) is True
    assert w.baseline == 29.4
    assert w.heated_for(minutes(20)) == timedelta(0)
    # Still being heated: counted on from the move, with no new start needed.
    assert w.due_in(minutes(25)) == timedelta(minutes=25)


def test_a_move_down_is_a_move():
    """A tank drawn from harder than it is heated falls: a live sensor."""
    w = watch()
    w.reading(T0, 40.0)
    w.heating(T0, True)
    assert w.reading(minutes(20), 39.5) is True
    assert w.heated_for(minutes(20)) == timedelta(0)


def test_less_than_the_band_is_not_a_move():
    """Measured from where the count started, not from the reading before."""
    w = watch()
    w.reading(T0, 28.86)
    w.heating(T0, True)
    for at, value in ((5, 28.9), (10, 28.86), (15, 29.1), (20, 29.35), (25, 28.4)):
        assert w.reading(minutes(at), value) is False
    assert w.baseline == 28.86
    assert w.overdue(minutes(30))


def test_exactly_the_band_is_a_move():
    w = watch()
    w.reading(T0, 28.0)
    assert not w.moved(28.49)
    assert w.moved(28.5)
    assert w.moved(27.5)


def test_exactly_the_band_survives_float_arithmetic():
    w = watch()
    w.reading(T0, 31.8)
    assert w.moved(32.3)
    assert not w.moved(32.29)


def test_a_count_restored_stands_if_the_reading_has_not_moved():
    w = watch()
    w.restore(28.86, 25 * 60)
    assert w.reading(T0, 28.86) is False
    w.heating(T0, True)
    assert w.due_in(T0) == timedelta(minutes=5)


def test_a_count_restored_is_dropped_by_a_reading_that_moved():
    w = watch()
    w.restore(28.86, 25 * 60)
    assert w.reading(T0, 36.0) is True
    w.heating(T0, True)
    assert w.due_in(T0) == LIMIT


def test_a_restored_count_is_bounded_and_not_running():
    w = watch()
    w.restore(28.86, 9 * 3600)
    assert w.heated_for(T0) == LIMIT
    assert w.due_in(T0) is None, "restored as heating"
    w.restore(28.86, -300)
    assert w.heated_for(T0) == timedelta(0)
    # Stored data is read back as any finite number: no overflow.
    w.restore(28.86, 1e300)
    assert w.heated_for(T0) == LIMIT


def test_a_restore_replaces_whatever_was_being_counted():
    w = watch()
    w.reading(T0, 50.0)
    w.heating(T0, True)
    w.restore(28.86, 5 * 60)
    assert w.baseline == 28.86
    assert w.due_in(minutes(20)) is None, "still running the stretch it had before"
    assert w.heated_for(minutes(20)) == timedelta(minutes=5)


def test_a_restart_counts_from_zero():
    w = watch()
    w.reading(T0, 28.86)
    w.heating(T0, True)
    w.restart(minutes(40), 28.86)
    assert not w.overdue(minutes(60))
    assert w.overdue(minutes(70))


def test_a_restart_with_no_value_waits_for_a_reading():
    w = watch()
    w.reading(T0, 28.86)
    w.heating(T0, True)
    w.restart(minutes(40), None)
    assert w.due_in(minutes(50)) is None
    assert w.reading(minutes(50), 28.86) is True
    assert w.due_in(minutes(50)) == LIMIT


def test_a_clock_that_steps_back_counts_nothing_negative():
    w = watch()
    w.reading(T0, 28.86)
    w.heating(minutes(10), True)
    assert w.heated_for(T0) == timedelta(0)
    w.heating(T0, False)
    assert w.heated_for(minutes(10)) == timedelta(0)
