"""Unit tests for the fleet's surplus fit (4.7.0).

Heat that runs only because a PV surplus was announced has to fit that surplus.
The numbers are the two real heaters' (1850 W upstairs, 1190 W downstairs) and
the times are seconds from 13:20:53 on 2026-11-23, when both started on a
surplus that could carry one of them and both lost it 21 minutes later.

Pure logic, no Home Assistant: the heater's side is in test_surplus_fit.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.generic_water_heater.fleet import (
    NO_READING,
    SURPLUS_ARBITRATION_SECONDS,
    SURPLUS_RETRY_SECONDS,
    SURPLUS_SETTLE_SECONDS,
    SURPLUS_SHORT_HOLD_LAST_SECONDS,
    SURPLUS_SHORT_HOLD_SECONDS,
    SURPLUS_SHORT_TOLERANCE_W,
    SURPLUS_UNKNOWN_DRAW_W,
    SURPLUS_WANT_TTL_SECONDS,
    SURPLUS_YIELD_SECONDS,
    HeaterFleet,
    SurplusReading,
    surplus_fits,
)

UP = "01JQ0000000000000000UPSTRS"
DOWN = "01JQ0000000000000000DWNSTR"
UP_W = 1850.0
DOWN_W = 1190.0

ARMED = datetime(2026, 11, 23, 11, 20, 53, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return ARMED + timedelta(seconds=seconds)


def spare(watts: float, curtailed: bool = False) -> SurplusReading:
    return SurplusReading(watts, curtailed)


def build(*entries) -> HeaterFleet:
    fleet = HeaterFleet()
    for entry_id, name in entries or ((UP, "Upstairs"), (DOWN, "Downstairs")):
        fleet.register(entry_id, name, stagger_seconds=0)
    return fleet


def ask(fleet, entry_id, seconds, reading, *, draw=None, distance=10.0, extra=False):
    if draw is None:
        draw = UP_W if entry_id == UP else DOWN_W
    return fleet.request_surplus(
        entry_id, at(seconds), reading=reading, draw_w=draw, distance=distance, extra=extra
    )


def admit(fleet, entry_id, seconds, reading=spare(5000)):
    """Admit past the arbitration window and report the element drawing."""
    ask(fleet, entry_id, seconds, reading)
    decision = ask(fleet, entry_id, seconds + SURPLUS_ARBITRATION_SECONDS, reading)
    assert decision.admitted, decision.reason
    fleet.note_switch_on(entry_id, at(seconds + SURPLUS_ARBITRATION_SECONDS))


# --- surplus_fits -----------------------------------------------------------


@pytest.mark.parametrize(
    ("reading", "draw", "fits"),
    [
        (spare(2036), UP_W, True),            # the draw and a tenth over
        (spare(2034), UP_W, False),
        (spare(650), DOWN_W, False),
        (spare(0, curtailed=True), UP_W, True),      # held back: a probe
        (spare(-400, curtailed=True), UP_W, True),
        (NO_READING, UP_W, True),             # no reading is not a refusal
        (spare(2199), None, False),           # an unknown draw is taken as 2000
        (spare(2200), None, True),
        (spare(2200), 0, True),
    ],
)
def test_surplus_fits(reading, draw, fits):
    assert surplus_fits(reading, draw) is fits


def test_the_unknown_draw_is_a_typical_element():
    assert SURPLUS_UNKNOWN_DRAW_W == 2000.0


# --- one heater ---------------------------------------------------------------


def test_a_single_heater_that_fits_starts_at_once():
    fleet = build((UP, "Upstairs"))
    assert ask(fleet, UP, 0, spare(2100)).admitted
    assert fleet.holds_surplus(UP)


def test_a_single_heater_that_does_not_fit_waits_and_says_why():
    fleet = build((UP, "Upstairs"))
    decision = ask(fleet, UP, 0, spare(1300))
    assert not decision.admitted
    assert decision.retry_after == SURPLUS_RETRY_SECONDS
    # The reason is published, so it carries no number: a number would write
    # a recorder row with every change of the reading for as long as it waits.
    assert decision.reason == "surplus: not enough to carry this heater"
    assert not fleet.holds_surplus(UP)
    # And is admitted as soon as it does: a refusal is a delay.
    assert ask(fleet, UP, 60, spare(2100)).admitted


def test_an_unregistered_heater_is_never_blocked():
    fleet = HeaterFleet()
    assert ask(fleet, UP, 0, spare(-900)).admitted
    assert not fleet.holds_surplus(UP)


def test_asking_again_while_admitted_is_free():
    fleet = build((UP, "Upstairs"))
    assert ask(fleet, UP, 0, spare(2100)).admitted
    # The reading now shows its own draw; that must not un-admit it.
    assert ask(fleet, UP, 30, spare(100)).admitted


# --- two heaters asking in one pass ------------------------------------------


def test_the_first_to_ask_waits_for_the_others_requests():
    fleet = build()
    decision = ask(fleet, UP, 0, spare(5000), distance=19.2)
    assert not decision.admitted
    assert decision.retry_after == pytest.approx(SURPLUS_ARBITRATION_SECONDS)
    assert "collecting" in decision.reason


def test_the_tank_closest_to_its_goal_goes_first():
    """Upstairs's listener ran first; downstairs has 14.4 C to go against
    upstairs's 19.2. One tank at its target is hot water; two halfway are not."""
    fleet = build()
    ask(fleet, UP, 0, spare(5000), distance=19.2)
    ask(fleet, DOWN, 0.001, spare(5000), distance=14.4)

    yielded = ask(fleet, UP, 2.0, spare(5000), distance=19.2)
    assert not yielded.admitted
    assert yielded.retry_after == SURPLUS_YIELD_SECONDS
    assert yielded.reason == "surplus: a heater closer to its goal goes first"
    assert ask(fleet, DOWN, 2.001, spare(5000), distance=14.4).admitted


def test_a_tank_short_of_its_normal_target_goes_before_any_boost():
    """Downstairs is 2 C from its boost target and already has its normal one;
    upstairs is 19 C from hot water. Closest-first does not mean topping up a
    tank that is already hot while the other is cold."""
    fleet = build()
    ask(fleet, DOWN, 0, spare(5000), distance=2.0, extra=True)
    ask(fleet, UP, 0.001, spare(5000), distance=19.2)
    assert not ask(fleet, DOWN, 2.0, spare(5000), distance=2.0, extra=True).admitted
    assert ask(fleet, UP, 2.001, spare(5000), distance=19.2).admitted


def test_between_two_boosts_the_closer_goes_first():
    fleet = build()
    ask(fleet, UP, 0, spare(5000), distance=12.0, extra=True)
    ask(fleet, DOWN, 0.001, spare(5000), distance=3.0, extra=True)
    assert not ask(fleet, UP, 2.0, spare(5000), distance=12.0, extra=True).admitted
    assert ask(fleet, DOWN, 2.001, spare(5000), distance=3.0, extra=True).admitted


@pytest.mark.parametrize("first", [UP, DOWN])
def test_equal_distances_do_not_deadlock(first):
    """One goes, and which one does not depend on who asked first."""
    second = DOWN if first == UP else UP
    fleet = build()
    ask(fleet, first, 0, spare(5000), distance=10)
    ask(fleet, second, 0, spare(5000), distance=10)
    decisions = {
        first: ask(fleet, first, 2, spare(5000), distance=10),
        second: ask(fleet, second, 2, spare(5000), distance=10),
    }
    assert sum(d.admitted for d in decisions.values()) == 1
    assert decisions[min(UP, DOWN)].admitted


def test_a_request_is_judged_on_what_it_says_now():
    """Downstairs asked while it was the closer; by its next ask a hot-water
    draw has left it further from its goal than upstairs. The newer numbers
    decide."""
    fleet = build()
    ask(fleet, DOWN, 0, spare(5000), distance=14.4)
    ask(fleet, UP, 0, spare(5000), distance=19.2)
    assert not ask(fleet, DOWN, 2, spare(5000), distance=25.0).admitted
    assert ask(fleet, UP, 2, spare(5000), distance=19.2).admitted


def test_the_surplus_is_not_left_idle_for_a_heater_that_does_not_fit():
    """1400 W spare: upstairs is the closer but draws 1850 W and cannot use it;
    downstairs can."""
    fleet = build()
    ask(fleet, UP, 0, spare(1400), distance=5.0)
    ask(fleet, DOWN, 0, spare(1400), distance=14.4)
    assert not ask(fleet, UP, 2, spare(1400), distance=5.0).admitted
    assert ask(fleet, DOWN, 2, spare(1400), distance=14.4).admitted
    assert fleet.holds_surplus(DOWN) and not fleet.holds_surplus(UP)


def test_a_heater_that_stopped_asking_stops_being_waited_for():
    fleet = build()
    ask(fleet, UP, 0, spare(5000), distance=5.0)
    later = SURPLUS_WANT_TTL_SECONDS + 1
    # Upstairs, the closer one, never came back. Downstairs opens its own
    # window and gets in.
    assert not ask(fleet, DOWN, later, spare(5000), distance=14.4).admitted
    assert ask(
        fleet, DOWN, later + SURPLUS_ARBITRATION_SECONDS, spare(5000), distance=14.4
    ).admitted


# --- one at a time ----------------------------------------------------------


def test_nobody_is_admitted_on_a_reading_older_than_the_last_start():
    """The reading that let upstairs in does not yet show upstairs's draw."""
    fleet = build()
    admit(fleet, UP, 0)
    held = ask(fleet, DOWN, 10, spare(5000))
    assert not held.admitted
    assert "settle" in held.reason
    assert held.retry_after == pytest.approx(SURPLUS_SETTLE_SECONDS - 8)
    assert ask(fleet, DOWN, 2 + SURPLUS_SETTLE_SECONDS, spare(3000)).admitted


def test_the_settle_runs_from_the_element_starting_not_from_the_admission():
    """A stagger hold can sit between the two: the load shows up after it."""
    fleet = build()
    ask(fleet, UP, 0, spare(5000))
    assert ask(fleet, UP, 2, spare(5000)).admitted
    ask(fleet, DOWN, 10, spare(5000))         # downstairs has been asking
    fleet.note_switch_on(UP, at(50))          # upstairs's relay closed 48 s later
    # 78 s after the admission, 30 s after the load appeared: still settling.
    held = ask(fleet, DOWN, 80, spare(5000))
    assert not held.admitted and "settle" in held.reason
    assert held.retry_after == pytest.approx(SURPLUS_SETTLE_SECONDS - 30)
    assert ask(fleet, DOWN, 50 + SURPLUS_SETTLE_SECONDS, spare(3000)).admitted


def test_repeated_on_reports_do_not_restart_the_settle():
    fleet = build()
    admit(fleet, UP, 0)
    fleet.note_switch_on(UP, at(40))          # an attribute update
    after = 2 + SURPLUS_SETTLE_SECONDS
    # Its first ask only opens the request window; the settle is already over.
    assert "settle" not in ask(fleet, DOWN, after, spare(3000)).reason
    assert ask(fleet, DOWN, after + SURPLUS_ARBITRATION_SECONDS, spare(3000)).admitted


def test_the_second_heater_is_sized_against_what_is_left():
    """2026-11-23 13:20: 3.8 kW of sun, 1.3 kW of house. Downstairs is the closer
    and starts; with it running 1310 W is left, and upstairs (1850 W) does not
    fit it."""
    fleet = build()
    ask(fleet, UP, 0, spare(2500), distance=19.2)
    ask(fleet, DOWN, 0, spare(2500), distance=14.4)
    assert ask(fleet, DOWN, 2, spare(2500), distance=14.4).admitted
    fleet.note_switch_on(DOWN, at(2))
    decision = ask(fleet, UP, 70, spare(1310), distance=19.2)
    assert not decision.admitted
    assert "not enough" in decision.reason


# --- the reading goes short ---------------------------------------------------


def both_on(fleet):
    admit(fleet, UP, 0)
    admit(fleet, DOWN, 100)                   # downstairs is the last in
    return 102                                # its element started here


def due(fleet, entry_id, seconds, reading):
    return fleet.surplus_release_due(entry_id, at(seconds), reading)


def test_a_shortfall_inside_the_tolerance_releases_nobody():
    fleet = build()
    both_on(fleet)
    reading = spare(-SURPLUS_SHORT_TOLERANCE_W)
    assert due(fleet, DOWN, 1000, reading) == (False, None)
    assert due(fleet, DOWN, 2000, reading) == (False, None)


def test_a_short_reading_must_hold_before_anyone_goes():
    fleet = build()
    both_on(fleet)
    release, again = due(fleet, DOWN, 1000, spare(-540))
    assert not release and again == pytest.approx(SURPLUS_SHORT_HOLD_SECONDS)
    release, again = due(fleet, DOWN, 1059, spare(-540))
    assert not release and again == pytest.approx(1)


def test_the_last_one_in_goes_first_and_alone():
    fleet = build()
    both_on(fleet)
    due(fleet, UP, 1000, spare(-540))
    assert due(fleet, DOWN, 1060, spare(-540)) == (True, None)
    # Upstairs is told it is not its turn, and when to look again.
    assert due(fleet, UP, 1060, spare(-540)) == (False, SURPLUS_SETTLE_SECONDS)


def test_a_recovered_reading_starts_the_hold_again():
    fleet = build()
    both_on(fleet)
    due(fleet, DOWN, 1000, spare(-540))
    due(fleet, DOWN, 1040, spare(200))        # a kettle went off
    release, again = due(fleet, DOWN, 1061, spare(-540))
    assert not release and again == pytest.approx(SURPLUS_SHORT_HOLD_SECONDS)


def test_after_one_release_the_next_waits_for_the_reading_to_settle():
    fleet = build()
    both_on(fleet)
    due(fleet, DOWN, 1000, spare(-900))
    assert due(fleet, DOWN, 1060, spare(-900))[0]
    fleet.release_surplus(DOWN, at(1060))
    # Still short at once: upstairs's turn, but not on a reading that still
    # has downstairs's draw in it - and as the last one left it waits longer.
    release, again = due(fleet, UP, 1061, spare(-900))
    assert not release
    assert again == pytest.approx(SURPLUS_SHORT_HOLD_LAST_SECONDS - 61)
    assert due(fleet, UP, 1000 + SURPLUS_SHORT_HOLD_LAST_SECONDS, spare(-900)) == (
        True,
        None,
    )


def test_three_heaters_go_one_settle_apart_until_one_is_left():
    third = "01JQ0000000000000000KITCHN"
    fleet = build((UP, "Upstairs"), (DOWN, "Downstairs"), (third, "Kitchen"))
    admit(fleet, UP, 0)
    admit(fleet, DOWN, 100)
    admit(fleet, third, 200)
    due(fleet, UP, 1000, spare(-900))
    assert due(fleet, third, 1060, spare(-900)) == (True, None)
    fleet.release_surplus(third, at(1060))
    assert not due(fleet, DOWN, 1119, spare(-900))[0]
    assert due(fleet, DOWN, 1120, spare(-900)) == (True, None)


def test_the_last_heater_left_rides_out_a_kettle():
    """Nobody to save by going early: it waits two and a half minutes, not one."""
    fleet = build()
    admit(fleet, UP, 0)
    due(fleet, UP, 1000, spare(-700))
    release, again = due(fleet, UP, 1000 + SURPLUS_SHORT_HOLD_SECONDS, spare(-700))
    assert not release
    assert again == pytest.approx(
        SURPLUS_SHORT_HOLD_LAST_SECONDS - SURPLUS_SHORT_HOLD_SECONDS
    )
    assert due(fleet, UP, 1000 + SURPLUS_SHORT_HOLD_LAST_SECONDS, spare(-700))[0]


def test_a_heater_told_to_go_is_counted_until_its_relay_is_off():
    """Its minimum on-time holds the relay closed. Its load is still in the
    reading, so the reading stays short - and that is not the sibling's fault."""
    fleet = build()
    both_on(fleet)
    due(fleet, UP, 1000, spare(-540))
    assert due(fleet, DOWN, 1060, spare(-540))[0]
    fleet.mark_surplus_releasing(DOWN)
    assert fleet.holds_surplus(DOWN) and fleet.surplus_releasing(DOWN)

    # Ten minutes of the same short reading: nobody else goes, and the one
    # going is not told again.
    for seconds in (1061, 1120, 1300, 1660):
        assert due(fleet, UP, seconds, spare(-540)) == (False, SURPLUS_SETTLE_SECONDS)
        assert due(fleet, DOWN, seconds, spare(-540)) == (False, SURPLUS_SETTLE_SECONDS)

    # The relay opens. Now it is upstairs's reading to answer, after a settle
    # and its own (longer) hold.
    fleet.release_surplus(DOWN, at(1660))
    assert not fleet.holds_surplus(DOWN)
    assert not due(fleet, UP, 1700, spare(-540))[0]
    assert due(fleet, UP, 1660 + SURPLUS_SETTLE_SECONDS, spare(-540))[0]


def test_a_heater_told_to_go_that_comes_to_depend_on_the_surplus_again_is_counted():
    """Its wait ran out with the relay still held on."""
    fleet = build()
    both_on(fleet)
    fleet.mark_surplus_releasing(DOWN)
    fleet.adopt_surplus(DOWN, at(3000))
    assert not fleet.surplus_releasing(DOWN)
    due(fleet, UP, 4000, spare(-540))
    assert due(fleet, DOWN, 4060, spare(-540))[0]          # still the last in


def test_a_release_right_after_a_start_waits_out_the_settle():
    """The reading dips while the array catches up with a new load."""
    fleet = build()
    started = both_on(fleet)
    release, again = due(fleet, DOWN, started + 5, spare(-300))
    assert not release
    # Whichever is later: the hold from the dip, or the settle from the start.
    assert again == pytest.approx(SURPLUS_SHORT_HOLD_SECONDS)
    assert due(fleet, DOWN, started + 5 + SURPLUS_SHORT_HOLD_SECONDS, spare(-300))[0]


def test_a_heater_that_is_not_counted_is_never_asked_to_go():
    fleet = build()
    admit(fleet, UP, 0)
    assert due(fleet, DOWN, 1000, spare(-900)) == (False, None)
    assert due(fleet, DOWN, 2000, spare(-900)) == (False, None)


def test_no_reading_releases_nobody():
    fleet = build()
    both_on(fleet)
    due(fleet, DOWN, 1000, spare(-900))
    assert due(fleet, DOWN, 1100, NO_READING) == (False, None)
    # And the hold starts again when the reading is back.
    assert not due(fleet, DOWN, 1101, spare(-900))[0]


# --- who is counted -----------------------------------------------------------


def test_a_heater_found_heating_on_the_surplus_is_counted_and_goes_first():
    """After a restart nobody remembers who was admitted when. A heater with no
    known start is dated from when it was found."""
    fleet = build()
    admit(fleet, UP, 0)
    fleet.adopt_surplus(DOWN, at(500))
    assert fleet.holds_surplus(DOWN)
    due(fleet, UP, 1000, spare(-540))
    assert due(fleet, DOWN, 1060, spare(-540))[0]
    assert not due(fleet, UP, 1060, spare(-540))[0]


def test_a_heater_found_heating_is_dated_from_when_its_element_started():
    """Across a reload the stagger still knows when each element last started.
    Downstairs has been on since 10 s; upstairs was admitted at 100 s. A reload
    of downstairs must not make it the last in, nor its run a short one."""
    fleet = build()
    assert fleet.request_turn_on(DOWN, at(10)).admitted
    admit(fleet, UP, 100)
    fleet.adopt_surplus(DOWN, at(5000))
    assert fleet.surplus_drawing_since(DOWN) == at(10)
    due(fleet, DOWN, 6000, spare(-540))
    assert due(fleet, UP, 6060, spare(-540))[0]
    assert not due(fleet, DOWN, 6060, spare(-540))[0]


def test_a_start_stamped_in_the_future_is_not_believed():
    """The clock stepped back after the element started. Dated from then it
    would be the last in until the clock caught up, and its run "not begun"."""
    fleet = build()
    assert fleet.request_turn_on(DOWN, at(7200)).admitted
    fleet.adopt_surplus(DOWN, at(100))
    assert fleet.surplus_drawing_since(DOWN) == at(100)


def test_adopting_twice_does_not_move_a_heater_to_the_back():
    fleet = build()
    fleet.adopt_surplus(UP, at(0))
    fleet.adopt_surplus(DOWN, at(10))
    fleet.adopt_surplus(UP, at(20))
    due(fleet, UP, 1000, spare(-540))
    assert due(fleet, DOWN, 1060, spare(-540))[0]


def test_an_unregistered_heater_cannot_be_adopted():
    fleet = build((UP, "Upstairs"))
    fleet.adopt_surplus(DOWN, at(0))
    assert not fleet.holds_surplus(DOWN)


def test_releasing_a_heater_that_never_started_does_not_hold_the_others_up():
    fleet = build()
    ask(fleet, UP, 0, spare(5000))
    assert ask(fleet, UP, 2, spare(5000)).admitted      # admitted, relay not yet on
    fleet.release_surplus(UP, at(3))                    # control changed its mind
    # No load left the supply, but the admission's own stamp still stands.
    assert not ask(fleet, DOWN, 10, spare(5000)).admitted
    assert ask(fleet, DOWN, 2 + SURPLUS_SETTLE_SECONDS, spare(5000)).admitted


def test_a_heater_stopping_lets_the_reading_settle_before_the_next_start():
    fleet = build()
    admit(fleet, UP, 0)
    fleet.release_surplus(UP, at(900))                  # its tank is satisfied
    held = ask(fleet, DOWN, 905, spare(600))
    assert not held.admitted and "settle" in held.reason


def test_unregistering_takes_the_heaters_place_with_it():
    fleet = build()
    admit(fleet, UP, 0)
    ask(fleet, DOWN, 200, spare(100))
    fleet.unregister(UP, at(300))
    fleet.unregister(DOWN, at(300))
    snapshot = fleet.surplus_snapshot()
    assert snapshot["claims"] == {} and snapshot["wants"] == {}


def test_releasing_also_withdraws_a_request():
    """Upstairs asked, would fit and is the closer, and then stopped wanting
    heat (its control pass settled on off). It must not stand in front of
    downstairs for the three minutes a forgotten request takes to expire."""
    fleet = build()
    ask(fleet, UP, 0, spare(5000), distance=5.0)
    ask(fleet, DOWN, 0, spare(5000), distance=14.4)
    fleet.release_surplus(UP, at(1))
    assert ask(fleet, DOWN, SURPLUS_ARBITRATION_SECONDS, spare(5000), distance=14.4).admitted


def test_a_clock_stepping_back_does_not_wedge_the_fit():
    fleet = build()
    admit(fleet, UP, 3600)
    # The clock is now an hour earlier than the stamps: no settle is owed to a
    # change that has not happened yet, so only the request window is waited.
    ask(fleet, DOWN, 0, spare(5000))
    assert ask(fleet, DOWN, SURPLUS_ARBITRATION_SECONDS, spare(5000)).admitted
    assert not due(fleet, DOWN, 3, spare(-900))[0]


def test_a_clock_stepping_back_does_not_hold_a_release_for_an_hour():
    fleet = build()
    both_on(fleet)                                      # stamps around 100 s
    due(fleet, DOWN, 7200, spare(-540))                 # short since 7200
    # The clock is now two hours earlier than that. The hold starts again from
    # here instead of waiting for the clock to catch up.
    release, again = due(fleet, DOWN, 200, spare(-540))
    assert not release and again == pytest.approx(SURPLUS_SHORT_HOLD_SECONDS)
    assert due(fleet, DOWN, 200 + SURPLUS_SHORT_HOLD_SECONDS, spare(-540))[0]


def test_a_settle_stamp_in_the_future_does_not_hold_a_release():
    fleet = build()
    admit(fleet, UP, 0)
    admit(fleet, DOWN, 7200)                            # stamped two hours ahead
    due(fleet, DOWN, 100, spare(-540))
    assert due(fleet, DOWN, 100 + SURPLUS_SHORT_HOLD_SECONDS, spare(-540))[0]


def test_a_request_window_opened_in_the_future_is_not_waited_for():
    fleet = build()
    ask(fleet, UP, 3600, spare(5000))                   # then the clock steps back
    # Its own window "opened" an hour from now. It is not waited out.
    assert ask(fleet, UP, 0, spare(5000)).admitted


def test_the_stagger_is_untouched_by_the_fit():
    """Two heaters with no surplus power sensor never call request_surplus, and
    the stagger answers exactly as it did."""
    fleet = HeaterFleet()
    fleet.register(UP, "Upstairs", stagger_seconds=60)
    fleet.register(DOWN, "Downstairs", stagger_seconds=60)
    assert fleet.request_turn_on(UP, at(0)).admitted
    held = fleet.request_turn_on(DOWN, at(0.447))
    assert not held.admitted and "stagger" in held.reason
    assert fleet.surplus_snapshot()["claims"] == {}
