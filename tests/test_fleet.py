"""Unit tests for fleet-wide switch-on staggering.

The scenario throughout is the one that caused the 2026-08-18 overload: two
water heater instances share one eco template, so Home Assistant fires both of
their listeners in a single event-loop pass and the two switches get commanded
447 ms apart. Timestamps below start at the real trigger time and the real
switch offset so the tests read as that incident replayed.

Nameplate-watt admission used to live here too and was removed in 2.0.0 -- the
Power Load Balancer integration owns power balancing, including its own
nameplates. What remains is sequencing: never let two elements step on together.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.generic_water_heater.fleet import (
    DEFAULT_STAGGER_SECONDS,
    HeaterFleet,
)

UPSTAIRS = "01JQ0000000000000000UPSTRS"
DOWNSTAIRS = "01JQ0000000000000000DWNSTR"

# 13:51:26.785, when the PV-excess sensor turned on and both instances fired.
TRIGGER = datetime(2026, 8, 18, 13, 51, 26, 785000, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    """Return a timestamp ``seconds`` after the shared trigger."""
    return TRIGGER + timedelta(seconds=seconds)


def build_fleet(stagger_seconds: float = 60.0) -> HeaterFleet:
    """Return a fleet with the two real heaters registered."""
    fleet = HeaterFleet()
    fleet.register(
        UPSTAIRS, "Upstairs Water Heater", stagger_seconds=stagger_seconds
    )
    fleet.register(
        DOWNSTAIRS, "Downstairs Water Heater", stagger_seconds=stagger_seconds
    )
    return fleet


# ---------------------------------------------------------------------------
# Two instances requesting ON in the same event-loop pass
# ---------------------------------------------------------------------------


def test_simultaneous_requests_admit_only_one():
    """The 447 ms double switch-on must not happen: one goes, one waits."""
    fleet = build_fleet()

    first = fleet.request_turn_on(UPSTAIRS, at(0))
    second = fleet.request_turn_on(DOWNSTAIRS, at(0.447))

    assert first.admitted
    assert not second.admitted
    assert "stagger" in second.reason
    # The blocked instance is told exactly when to come back, and re-queues on
    # the existing cooldown-timer path rather than being dropped.
    assert second.retry_after == pytest.approx(60 - 0.447)


def test_deferred_instance_is_admitted_once_the_stagger_expires():
    """Deferral is a delay, never a cancellation."""
    fleet = build_fleet()
    fleet.request_turn_on(UPSTAIRS, at(0))

    assert not fleet.request_turn_on(DOWNSTAIRS, at(59)).admitted
    assert fleet.request_turn_on(DOWNSTAIRS, at(60.5)).admitted


def test_the_window_is_measured_from_the_sibling_not_from_the_first_ask():
    """A repeatedly-refused heater must not reset anyone's window."""
    fleet = build_fleet()
    fleet.request_turn_on(UPSTAIRS, at(0))

    for offset in (10, 20, 30, 40, 50):
        assert not fleet.request_turn_on(DOWNSTAIRS, at(offset)).admitted

    # Still admitted at 60 s after UPSTAIRS went on, not 60 s after the last ask.
    assert fleet.request_turn_on(DOWNSTAIRS, at(60.1)).admitted


def test_a_lone_heater_has_nothing_to_stagger_against():
    """A single-heater install must never be delayed."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs Water Heater")

    for offset in (0, 1, 2, 30):
        assert fleet.request_turn_on(UPSTAIRS, at(offset)).admitted


def test_a_heater_added_later_is_spaced_against_one_already_running():
    """The lone-heater path still has to record its admission.

    Otherwise the second entry to load sees a sibling with no history and is
    waved straight through -- which is the 2026-08-18 step, just deferred to
    whenever someone adds a tank.
    """
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs Water Heater")
    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted

    fleet.register(DOWNSTAIRS, "Downstairs Water Heater")

    assert not fleet.request_turn_on(DOWNSTAIRS, at(5)).admitted


def test_zero_stagger_disables_spacing():
    """0 is documented as "off", not as "space by zero seconds"."""
    fleet = build_fleet(stagger_seconds=0)

    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted
    assert fleet.request_turn_on(DOWNSTAIRS, at(0.447)).admitted


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


def test_unknown_entry_is_never_blocked():
    """A platform still setting up must not have its heating gated."""
    fleet = build_fleet()
    fleet.request_turn_on(UPSTAIRS, at(0))

    assert fleet.request_turn_on("01JQ00000000000000NOTHERE", at(1)).admitted


def test_unregistering_the_last_member_empties_the_fleet():
    """The shared fleet object outlives every entry, so this has to be clean."""
    fleet = build_fleet()
    fleet.unregister(UPSTAIRS)
    assert not fleet.is_empty
    fleet.unregister(DOWNSTAIRS)
    assert fleet.is_empty


def test_unregistering_removes_the_sibling_constraint():
    """One tank deleted means the other has nothing left to wait for."""
    fleet = build_fleet()
    fleet.request_turn_on(UPSTAIRS, at(0))
    assert not fleet.request_turn_on(DOWNSTAIRS, at(1)).admitted

    fleet.unregister(UPSTAIRS)

    assert fleet.request_turn_on(DOWNSTAIRS, at(2)).admitted


# ---------------------------------------------------------------------------
# Resolved settings
# ---------------------------------------------------------------------------


def test_stagger_resolves_to_the_longest_any_member_asks_for():
    """Each entry carries its own copy, so the fleet takes the safest value.

    Deterministic regardless of the order the entries happen to load in.
    """
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs Water Heater", stagger_seconds=30)
    fleet.register(DOWNSTAIRS, "Downstairs Water Heater", stagger_seconds=90)

    assert fleet.stagger_seconds == 90


def test_an_empty_fleet_reports_the_default_stagger():
    """Read before any entry registers; must not be 0, which means "off"."""
    assert HeaterFleet().stagger_seconds == DEFAULT_STAGGER_SECONDS


def test_garbage_config_values_fall_back_to_defaults():
    """Options come from a form; a None or a string must not crash admission."""
    fleet = HeaterFleet()
    fleet.register(UPSTAIRS, "Upstairs Water Heater", stagger_seconds=None)
    assert fleet.get(UPSTAIRS).stagger_seconds == DEFAULT_STAGGER_SECONDS

    fleet.register(UPSTAIRS, "Upstairs Water Heater", stagger_seconds="nonsense")
    assert fleet.get(UPSTAIRS).stagger_seconds == DEFAULT_STAGGER_SECONDS

    fleet.register(UPSTAIRS, "Upstairs Water Heater", stagger_seconds=-5)
    assert fleet.get(UPSTAIRS).stagger_seconds == 0


def test_a_decision_is_truthy_when_admitted():
    """`if decision:` is used at the call site."""
    fleet = build_fleet()
    assert fleet.request_turn_on(UPSTAIRS, at(0))
    assert not fleet.request_turn_on(DOWNSTAIRS, at(1))


# ---------------------------------------------------------------------------
# Regressions found reviewing the 2.0.0 removal
# ---------------------------------------------------------------------------


def test_an_externally_flipped_switch_starts_the_stagger_clock():
    """An ON this integration did not command still energises an element.

    The old module booked it via note_switch_state; removing the watts took
    that with it, which left a sibling free to step on top of a heater that a
    person, another automation, or a restart had found already running.
    """
    fleet = build_fleet()
    fleet.note_switch_on(UPSTAIRS, at(0))

    decision = fleet.request_turn_on(DOWNSTAIRS, at(5))

    assert not decision.admitted, (
        "a sibling was admitted on top of an already-energised element"
    )
    assert "stagger" in decision.reason


def test_re_asking_while_on_does_not_push_the_anchor_forward():
    """Otherwise a heater that keeps re-asking starves its sibling for ever.

    A switch stuck `unavailable` while genuinely drawing makes the caller
    re-ask on every control pass. If each ask re-anchored, the sibling's window
    would never elapse.
    """
    fleet = build_fleet()
    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted

    for offset in (10, 20, 30, 40, 50):
        fleet.request_turn_on(UPSTAIRS, at(offset))

    assert fleet.request_turn_on(DOWNSTAIRS, at(61)).admitted, (
        "the sibling was starved by a heater re-asking inside its own window"
    )


def test_repeated_observed_on_does_not_push_the_anchor_forward():
    """Same invariant, reached through the switch listener instead."""
    fleet = build_fleet()
    fleet.note_switch_on(UPSTAIRS, at(0))
    for offset in (10, 20, 30, 40, 50):
        fleet.note_switch_on(UPSTAIRS, at(offset))

    assert fleet.request_turn_on(DOWNSTAIRS, at(61)).admitted


def test_an_observed_off_lets_the_sibling_ask_again_normally():
    """OFF clears the belief, so the next ON is a real admission again."""
    fleet = build_fleet()
    fleet.note_switch_on(UPSTAIRS, at(0))
    fleet.note_switch_off(UPSTAIRS)

    # Still inside the window: the anchor is history, not belief.
    assert not fleet.request_turn_on(DOWNSTAIRS, at(5)).admitted
    # And upstairs asking again is a fresh admission, which re-anchors.
    assert fleet.request_turn_on(UPSTAIRS, at(70)).admitted
    assert not fleet.request_turn_on(DOWNSTAIRS, at(71)).admitted


def test_the_window_survives_the_reload_an_options_save_triggers():
    """The real reload path is unregister THEN register, not register alone.

    Preserving state on re-register is dead code for that path: the entity is
    removed first, which pops the member. An options save is precisely when
    both elements could otherwise come on together.
    """
    fleet = build_fleet()
    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted

    # Options saved on the upstairs entry: entity removed, then re-added.
    fleet.unregister(UPSTAIRS, at(2))
    fleet.register(UPSTAIRS, "Upstairs Water Heater", stagger_seconds=60)

    assert not fleet.request_turn_on(DOWNSTAIRS, at(5)).admitted, (
        "a reload cleared the stagger window"
    )


def test_a_stale_anchor_carried_through_a_reload_does_not_block_for_ever():
    """Carrying the anchor across a reload must not outlive its own window.

    Written after a first attempt at this test asserted the opposite -- that a
    re-registered entry starts with no anchor at all -- which contradicts the
    reload test above. Restoring a two-second-old anchor IS the reload case; the
    invariant that matters is that an old one stops mattering on time.
    """
    fleet = build_fleet()
    assert fleet.request_turn_on(UPSTAIRS, at(0)).admitted
    fleet.unregister(UPSTAIRS, at(1))
    fleet.register(UPSTAIRS, "Upstairs Water Heater", stagger_seconds=60)

    assert fleet.request_turn_on(DOWNSTAIRS, at(500)).admitted
