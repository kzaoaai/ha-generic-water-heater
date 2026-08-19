"""Unit tests for fleet-wide electrical admission control.

The scenario throughout is the one that caused the 2026-08-18 overload: two
water heater instances share one eco template, so Home Assistant fires both of
their listeners in a single event-loop pass and the two switches get commanded
447 ms apart. Timestamps below start at the real trigger time and the real
switch offset so the tests read as that incident replayed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.generic_water_heater.fleet import (
    ARBITRATION_SECONDS,
    BUDGET_RETRY_SECONDS,
    DEPARTED_COMMITMENT_SECONDS,
    PRIORITY_YIELD_SECONDS,
    UNAVAILABLE_COMMITMENT_SECONDS,
    UNCONFIRMED_COMMITMENT_SECONDS,
    WAITER_TTL_SECONDS,
    HeaterFleet,
)

UPSTAIRS = "01JQ0000000000000000UPSTRS"
DOWNSTAIRS = "01JQ0000000000000000DWNSTR"

UPSTAIRS_W = 2000.0
DOWNSTAIRS_W = 1300.0

# 13:51:26.785, when the PV-excess sensor turned on and both instances fired.
TRIGGER = datetime(2026, 8, 18, 13, 51, 26, 785000, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    """Return a timestamp ``seconds`` after the shared trigger."""
    return TRIGGER + timedelta(seconds=seconds)


def admit(fleet, entry_id, seconds, deficit=5.0, confirm=True):
    """Admit a heater the way the real thing does, arbitration window included.

    A real switch reports back within a second, which is what confirms the
    commitment. Tests that skip that step are testing a dead relay, not a
    working heater.
    """
    decision = fleet.request_turn_on(entry_id, at(seconds), deficit=deficit)
    if not decision.admitted:
        seconds += ARBITRATION_SECONDS
        decision = fleet.request_turn_on(entry_id, at(seconds), deficit=deficit)
    assert decision.admitted, f"expected admission, got {decision.reason}"
    if confirm:
        fleet.note_switch_state(entry_id, True, at(seconds + 1))
    return decision


def build_fleet(budget_w: float = 0.0, stagger_seconds: float = 60.0, **wakes):
    """Return a fleet with the two real heaters registered."""
    fleet = HeaterFleet()
    fleet.register(
        UPSTAIRS,
        "Upstairs Water Heater",
        nominal_power_w=UPSTAIRS_W,
        stagger_seconds=stagger_seconds,
        budget_w=budget_w,
        wake=wakes.get("upstairs_wake"),
    )
    fleet.register(
        DOWNSTAIRS,
        "Downstairs Water Heater",
        nominal_power_w=DOWNSTAIRS_W,
        stagger_seconds=stagger_seconds,
        budget_w=budget_w,
        wake=wakes.get("downstairs_wake"),
    )
    return fleet


# ---------------------------------------------------------------------------
# 1. Two instances requesting ON in the same event-loop pass
# ---------------------------------------------------------------------------


def test_simultaneous_requests_admit_only_one():
    """The 447 ms double switch-on must not happen: one goes, one waits."""
    fleet = build_fleet()

    first = fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)
    second = fleet.request_turn_on(DOWNSTAIRS, at(0.447), deficit=5.0)

    assert first.admitted
    assert not second.admitted
    assert "stagger" in second.reason
    # The blocked instance is told exactly when to come back, and re-queues on
    # the existing cooldown-timer path rather than being dropped.
    assert second.retry_after == pytest.approx(60 - 0.447)
    assert fleet.committed_power_w == UPSTAIRS_W


def test_deferred_instance_is_admitted_once_the_stagger_expires():
    """Deferral is a delay, never a cancellation."""
    fleet = build_fleet()
    fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)

    assert not fleet.request_turn_on(DOWNSTAIRS, at(59), deficit=5.0).admitted
    assert fleet.request_turn_on(DOWNSTAIRS, at(60.5), deficit=5.0).admitted
    assert fleet.committed_power_w == UPSTAIRS_W + DOWNSTAIRS_W


def test_stagger_only_counts_siblings():
    """A lone heater has nothing to stagger against."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs Water Heater", nominal_power_w=UPSTAIRS_W)

    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted
    fleet.release(UPSTAIRS, at(1))
    assert fleet.request_turn_on(UPSTAIRS, at(2)).admitted


def test_recommitting_an_already_on_heater_is_free():
    """Control passes re-affirm ON constantly; that must never self-block."""
    fleet = build_fleet()
    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted

    for offset in (1, 2, 30, 300):
        assert fleet.request_turn_on(UPSTAIRS, at(offset)).admitted

    assert fleet.committed_power_w == UPSTAIRS_W


# ---------------------------------------------------------------------------
# 2. Nameplate watt budget
# ---------------------------------------------------------------------------


def test_budget_refuses_the_combined_step():
    """3300 W of nameplate must not be admitted against a 2500 W budget."""
    fleet = build_fleet(budget_w=2500.0)

    # A budget too small for every heater at once opens the arbitration window,
    # so the first request is held briefly before it is admitted.
    held = fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)
    assert not held.admitted
    assert "arbitration" in held.reason
    assert fleet.request_turn_on(UPSTAIRS, at(ARBITRATION_SECONDS), deficit=5.0).admitted
    fleet.note_switch_state(UPSTAIRS, True, at(2))  # the switch reports back

    # Well past the stagger window, so only the budget can refuse this.
    fleet.request_turn_on(DOWNSTAIRS, at(200), deficit=5.0)
    blocked = fleet.request_turn_on(DOWNSTAIRS, at(201), deficit=5.0)

    assert not blocked.admitted
    assert "budget" in blocked.reason
    assert "3300" in blocked.reason and "2500" in blocked.reason
    assert blocked.retry_after == pytest.approx(BUDGET_RETRY_SECONDS)
    assert fleet.committed_power_w == UPSTAIRS_W


def test_budget_admits_what_actually_fits():
    """A heater small enough to fit under the budget is let through."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs", nominal_power_w=2000.0, budget_w=2500.0)
    fleet.register(DOWNSTAIRS, "Downstairs", nominal_power_w=500.0, budget_w=2500.0)

    admit(fleet, UPSTAIRS, 0)

    fleet.request_turn_on(DOWNSTAIRS, at(200))
    assert fleet.request_turn_on(DOWNSTAIRS, at(201)).admitted
    assert fleet.committed_power_w == 2500.0


def test_budget_never_blocks_a_heater_when_nothing_else_is_drawing():
    """The budget prevents an aggregate step; it never denies all hot water."""
    fleet = build_fleet(budget_w=1000.0)  # smaller than either heater

    fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)
    assert fleet.request_turn_on(UPSTAIRS, at(ARBITRATION_SECONDS), deficit=5.0).admitted


def test_manual_switch_on_is_charged_against_the_budget():
    """Watts drawn by a manual flip are real and must be visible to siblings."""
    fleet = build_fleet(budget_w=2500.0)

    fleet.note_switch_state(UPSTAIRS, True, at(0))
    assert fleet.committed_power_w == UPSTAIRS_W

    fleet.request_turn_on(DOWNSTAIRS, at(200), deficit=5.0)
    blocked = fleet.request_turn_on(DOWNSTAIRS, at(201), deficit=5.0)
    assert not blocked.admitted
    assert "budget" in blocked.reason


def test_switch_observed_off_releases_the_budget():
    """An externally driven OFF hands the watts straight back."""
    fleet = build_fleet(budget_w=2500.0)
    fleet.note_switch_state(UPSTAIRS, True, at(0))

    fleet.note_switch_state(UPSTAIRS, False, at(10))

    assert fleet.committed_power_w == 0.0


def test_unavailable_switch_keeps_holding_its_watts():
    """We cannot tell if an unavailable switch is drawing, so keep assuming it is."""
    fleet = build_fleet(budget_w=2500.0)
    fleet.note_switch_state(UPSTAIRS, True, at(0))

    # water_heater.py deliberately makes no note_switch_state call for an
    # unavailable switch, so the commitment survives.
    assert fleet.committed_power_w == UPSTAIRS_W


# ---------------------------------------------------------------------------
# 3. Deferral, then admission when capacity frees up
# ---------------------------------------------------------------------------


def test_release_admits_the_waiting_sibling():
    """Budget-blocked, then admitted the moment the other heater lets go."""
    woken = []
    fleet = build_fleet(budget_w=2500.0, downstairs_wake=lambda: woken.append("downstairs"))

    admit(fleet, UPSTAIRS, 0)
    fleet.request_turn_on(DOWNSTAIRS, at(200), deficit=8.0)
    assert not fleet.request_turn_on(DOWNSTAIRS, at(201), deficit=8.0).admitted

    fleet.release(UPSTAIRS, at(300))

    # The release wakes the waiter instead of leaving it on its backstop timer.
    assert woken == ["downstairs"]
    assert fleet.request_turn_on(DOWNSTAIRS, at(300), deficit=8.0).admitted
    assert fleet.committed_power_w == DOWNSTAIRS_W


def test_release_does_not_wake_anyone_when_nobody_is_waiting():
    """No spurious wake-ups for heaters that never asked."""
    woken = []
    fleet = build_fleet(budget_w=2500.0, downstairs_wake=lambda: woken.append("downstairs"))

    fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)
    fleet.request_turn_on(UPSTAIRS, at(1), deficit=5.0)
    fleet.release(UPSTAIRS, at(50))

    assert woken == []


def test_a_waiter_that_stopped_asking_stops_blocking():
    """A heater that quietly reached target must not hold priority forever."""
    fleet = build_fleet(budget_w=2500.0)

    # Downstairs waits once with a big deficit, then never asks again.
    fleet.request_turn_on(DOWNSTAIRS, at(0), deficit=20.0)

    stale = WAITER_TTL_SECONDS + BUDGET_RETRY_SECONDS * 2 + 10
    fleet.request_turn_on(UPSTAIRS, at(stale), deficit=1.0)
    admitted = fleet.request_turn_on(UPSTAIRS, at(stale + ARBITRATION_SECONDS), deficit=1.0)

    assert admitted.admitted


# ---------------------------------------------------------------------------
# 4. Unloading one entry while the other holds budget
# ---------------------------------------------------------------------------


def test_unregister_holds_the_watts_briefly_then_frees_them():
    """Unloading an entry does not switch its heater off, so do not hand its
    watts straight to a sibling -- but do not hold them for ever either."""
    fleet = build_fleet(budget_w=2500.0)

    admit(fleet, UPSTAIRS, 0)
    fleet.request_turn_on(DOWNSTAIRS, at(200), deficit=8.0)
    assert not fleet.request_turn_on(DOWNSTAIRS, at(201), deficit=8.0).admitted
    assert fleet.committed_power_w == UPSTAIRS_W

    fleet.unregister(UPSTAIRS, at(300))

    # The fleet object itself survives the unload and the survivor keeps its
    # state, but the departed element may still be energised.
    assert not fleet.is_empty
    assert fleet.get(DOWNSTAIRS) is not None
    assert fleet.get(UPSTAIRS) is None
    assert fleet.committed_power_w == UPSTAIRS_W
    assert not fleet.request_turn_on(DOWNSTAIRS, at(301), deficit=8.0).admitted

    # Once the grace period lapses the survivor gets the capacity.
    lapsed = 300 + DEPARTED_COMMITMENT_SECONDS + 1
    assert fleet.request_turn_on(DOWNSTAIRS, at(lapsed), deficit=8.0).admitted
    assert fleet.committed_power_w == DOWNSTAIRS_W


def test_unregister_does_not_wake_the_sibling():
    """An options save reloads an entry; the sibling must not pile in mid-reload."""
    woken = []
    fleet = build_fleet(budget_w=2500.0, downstairs_wake=lambda: woken.append("downstairs"))

    admit(fleet, UPSTAIRS, 0)
    fleet.request_turn_on(DOWNSTAIRS, at(200), deficit=8.0)
    fleet.request_turn_on(DOWNSTAIRS, at(201), deficit=8.0)

    fleet.unregister(UPSTAIRS, at(300))

    assert woken == [], "a sibling was invited into watts that may still be drawn"


def test_reload_keeps_the_running_element_accounted_for():
    """Unload then re-register (an options save) must not open a gap."""
    fleet = build_fleet(budget_w=2500.0)
    admit(fleet, UPSTAIRS, 0)

    fleet.unregister(UPSTAIRS, at(100))
    assert fleet.committed_power_w == UPSTAIRS_W, "watts vanished mid-reload"

    # The entry comes back and re-books what it is really drawing.
    fleet.register(UPSTAIRS, "Upstairs Water Heater", nominal_power_w=UPSTAIRS_W, budget_w=2500.0)
    fleet.note_switch_state(UPSTAIRS, True, at(101))

    assert fleet.committed_power_w == UPSTAIRS_W
    fleet.request_turn_on(DOWNSTAIRS, at(200), deficit=8.0)
    blocked = fleet.request_turn_on(DOWNSTAIRS, at(201), deficit=8.0)
    assert not blocked.admitted and "budget" in blocked.reason


# ---------------------------------------------------------------------------
# Commitments are claims about the world, and get reconciled against it
# ---------------------------------------------------------------------------


def test_a_switch_that_never_turns_on_stops_holding_the_budget():
    """A dead relay must not take the whole fleet's hot water with it."""
    fleet = build_fleet(budget_w=2500.0)

    # Admitted and commanded, but the switch never reports ON.
    admit(fleet, UPSTAIRS, 0, confirm=False)
    assert fleet.committed_power_w == UPSTAIRS_W

    blocked = fleet.request_turn_on(DOWNSTAIRS, at(60), deficit=30.0)
    assert not blocked.admitted

    lapsed = UNCONFIRMED_COMMITMENT_SECONDS + 10
    assert fleet.request_turn_on(DOWNSTAIRS, at(lapsed), deficit=30.0).admitted
    assert fleet.committed_power_w == DOWNSTAIRS_W


def test_an_unavailable_switch_holds_briefly_then_releases():
    """Killing a heater at the breaker must not starve its siblings for ever."""
    fleet = build_fleet(budget_w=2500.0)
    admit(fleet, UPSTAIRS, 0)

    # Power is cut upstream: the switch stops reporting entirely.
    fleet.note_switch_unavailable(UPSTAIRS, at(10))

    # Still held at first, because it might genuinely still be drawing.
    assert not fleet.request_turn_on(DOWNSTAIRS, at(120), deficit=30.0).admitted
    assert fleet.committed_power_w == UPSTAIRS_W

    lapsed = 10 + UNAVAILABLE_COMMITMENT_SECONDS + 1
    assert fleet.request_turn_on(DOWNSTAIRS, at(lapsed), deficit=30.0).admitted
    assert fleet.committed_power_w == DOWNSTAIRS_W


def test_a_confirmed_running_heater_is_never_expired():
    """A heater that really is on keeps its watts for as long as it runs."""
    fleet = build_fleet(budget_w=2500.0)
    admit(fleet, UPSTAIRS, 0)

    blocked = fleet.request_turn_on(DOWNSTAIRS, at(86400), deficit=30.0)
    blocked = fleet.request_turn_on(DOWNSTAIRS, at(86402), deficit=30.0)

    assert not blocked.admitted and "budget" in blocked.reason
    assert fleet.committed_power_w == UPSTAIRS_W


def test_an_expiring_commitment_wakes_a_waiting_sibling():
    """A stale claim lapsing should hand capacity over without waiting for the
    blocked heater's own backstop timer."""
    woken = []
    fleet = build_fleet(budget_w=2500.0, downstairs_wake=lambda: woken.append("downstairs"))
    third = "01JQ00000000000000000THIRD"
    fleet.register(third, "Third", nominal_power_w=100.0, budget_w=2500.0)

    admit(fleet, UPSTAIRS, 0, confirm=False)  # commanded, switch never reports ON
    fleet.request_turn_on(DOWNSTAIRS, at(60), deficit=30.0)  # parked on the budget
    woken.clear()

    # Any later fleet activity reconciles the stale claim and wakes the waiter.
    fleet.request_turn_on(third, at(UNCONFIRMED_COMMITMENT_SECONDS + 10), deficit=1.0)

    assert woken == ["downstairs"]


def test_a_requester_is_never_woken_by_its_own_reconciliation():
    """The requester is already here; waking it would just double the work."""
    woken = []
    fleet = build_fleet(budget_w=2500.0, downstairs_wake=lambda: woken.append("downstairs"))

    admit(fleet, UPSTAIRS, 0, confirm=False)
    fleet.request_turn_on(DOWNSTAIRS, at(60), deficit=30.0)
    woken.clear()

    admitted = fleet.request_turn_on(
        DOWNSTAIRS, at(UNCONFIRMED_COMMITMENT_SECONDS + 10), deficit=30.0
    )

    assert admitted.admitted
    assert woken == []


def test_unregistering_the_last_member_empties_the_fleet():
    """__init__ uses is_empty to decide when the shared object can be dropped."""
    fleet = build_fleet()
    fleet.unregister(UPSTAIRS, at(0))
    assert not fleet.is_empty
    fleet.unregister(DOWNSTAIRS, at(0))
    assert fleet.is_empty


def test_unknown_entry_is_never_blocked():
    """Missing bookkeeping must fail open, never withhold heat."""
    fleet = build_fleet(budget_w=100.0)
    assert fleet.request_turn_on("not-a-registered-entry", at(0)).admitted


# ---------------------------------------------------------------------------
# Priority by temperature deficit
# ---------------------------------------------------------------------------


def test_larger_deficit_wins_the_only_free_slot():
    """When only one heater fits, the colder tank goes first."""
    woken = []
    fleet = build_fleet(budget_w=2500.0, downstairs_wake=lambda: woken.append("downstairs"))

    # Both fire in the same event-loop pass. Upstairs is barely below target,
    # downstairs is far below it.
    assert not fleet.request_turn_on(UPSTAIRS, at(0), deficit=1.0).admitted
    assert not fleet.request_turn_on(DOWNSTAIRS, at(0.447), deficit=12.0).admitted

    # Upstairs comes back first, but yields to the colder sibling and wakes it.
    yielded = fleet.request_turn_on(UPSTAIRS, at(ARBITRATION_SECONDS), deficit=1.0)
    assert not yielded.admitted
    assert "priority" in yielded.reason
    assert yielded.retry_after == pytest.approx(PRIORITY_YIELD_SECONDS)
    assert woken == ["downstairs"]

    # The woken sibling takes the slot.
    assert fleet.request_turn_on(DOWNSTAIRS, at(ARBITRATION_SECONDS), deficit=12.0).admitted
    assert fleet.committed_power_w == DOWNSTAIRS_W


def test_capacity_is_not_idled_for_a_request_that_cannot_be_served():
    """Yielding only happens when it actually lets the higher priority in."""
    fleet = HeaterFleet()
    # Big is far below target but far too large for what is left of the budget.
    fleet.register(UPSTAIRS, "Big", nominal_power_w=4000.0, budget_w=3000.0)
    fleet.register(DOWNSTAIRS, "Small", nominal_power_w=500.0, budget_w=3000.0)
    other = "01JQ00000000000000000THIRD"
    fleet.register(other, "Incumbent", nominal_power_w=2400.0, budget_w=3000.0)

    fleet.note_switch_state(other, True, at(0))  # 2400 W committed, 600 W free

    fleet.request_turn_on(UPSTAIRS, at(100), deficit=30.0)  # 4000 W cannot ever fit
    blocked = fleet.request_turn_on(UPSTAIRS, at(101), deficit=30.0)
    assert not blocked.admitted

    admitted = fleet.request_turn_on(DOWNSTAIRS, at(102), deficit=2.0)

    assert admitted.admitted, "the 600 W of headroom should not sit idle"


def test_equal_deficits_do_not_deadlock():
    """Ties fall through to arrival order rather than yielding forever."""
    fleet = build_fleet(budget_w=2500.0)

    fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)
    fleet.request_turn_on(DOWNSTAIRS, at(0.447), deficit=5.0)

    assert fleet.request_turn_on(UPSTAIRS, at(ARBITRATION_SECONDS), deficit=5.0).admitted


def test_no_arbitration_window_when_the_budget_fits_everyone():
    """The 1 s hold only appears where contention is actually possible."""
    fleet = build_fleet(budget_w=10000.0)

    decision = fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)

    assert decision.admitted


# ---------------------------------------------------------------------------
# Resolution of per-entry settings into fleet-wide ones
# ---------------------------------------------------------------------------


def test_settings_resolve_to_the_most_conservative_value():
    """Order of entry loading must not change how the fleet behaves."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs", stagger_seconds=30.0, budget_w=4000.0)
    fleet.register(DOWNSTAIRS, "Downstairs", stagger_seconds=90.0, budget_w=2500.0)

    assert fleet.stagger_seconds == 90.0  # longest wins
    assert fleet.budget_w == 2500.0  # tightest wins


def test_budget_is_disabled_when_no_member_sets_one():
    """Zero means unlimited, and a zero must not win the min()."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs", budget_w=0.0)
    fleet.register(DOWNSTAIRS, "Downstairs", budget_w=2500.0)

    assert fleet.budget_w == 2500.0


def test_registering_again_preserves_committed_watts():
    """Re-registering on an options update must not lose the running state."""
    fleet = build_fleet()
    fleet.request_turn_on(UPSTAIRS, at(0), deficit=5.0)

    fleet.register(UPSTAIRS, "Upstairs Water Heater", nominal_power_w=2100.0)

    assert fleet.committed_power_w == 2100.0


def test_missing_nominal_power_warns_when_a_budget_is_active(caplog):
    """A heater invisible to the budget is a real hazard; say so loudly."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs", nominal_power_w=2000.0, budget_w=2500.0)
    fleet.register(DOWNSTAIRS, "Downstairs Water Heater", nominal_power_w=0.0)

    assert "Downstairs Water Heater" in caplog.text
    assert "nominal power" in caplog.text


def test_garbage_config_values_fall_back_to_defaults():
    """A bad option must not take the admission logic down with it."""
    fleet = HeaterFleet()
    member = fleet.register(
        UPSTAIRS, "Upstairs", nominal_power_w="not-a-number", stagger_seconds=-5, budget_w=None
    )

    assert member.nominal_power_w == 0.0
    assert member.stagger_seconds == 0.0
    assert member.budget_w == 0.0
