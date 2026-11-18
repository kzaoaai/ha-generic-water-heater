"""A surplus that could not carry the element does not count again for a while.

A surplus detector that judges from the battery sees the heater's own draw:
when the element needs more than the spare PV, the surplus clears a few
minutes after the element starts and returns a minute after it stops. Until
4.6.7 the short-run lockout withheld only the boost above the normal target.
A tank below its normal target, heated by the eco gate on the surplus,
followed every flap: on 2026-11-18 the surplus entity changed 15 times in 100
minutes and both relays switched with each change, six runs of about three
minutes with about a minute between them.

Since 4.6.8 a surplus lost within SURPLUS_BOOST_SHORT_RUN of the element
starting on it does not count toward the eco condition either, for
SURPLUS_BOOST_LOCKOUT. Heat that does not hang on the surplus (the utility,
the template) is never locked out.

The boost's side of the lockout is in test_surplus_boost.py.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.generic_water_heater import SMART_ECO_MODE_AUTO_RESUME
from custom_components.generic_water_heater.water_heater import (
    STARTUP_GRACE,
    SURPLUS_BOOST_LOCKOUT,
    SURPLUS_BOOST_SHORT_RUN,
)
from tests.test_eco_inputs import (
    SOURCE,
    UPSTAIRS,
    heater,
    set_state,
    setup_inputs,
    switch_on,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    entities,
    world,
)
from tests.test_restart_boost import finish_starting, restarted, say

T0 = datetime(2026, 11, 18, 12, 11, 35, tzinfo=timezone.utc)
WAITING = "Waiting after a short surplus"


def locked_until(hass):
    return heater(hass).attributes["surplus_boost_locked_until"]


async def tick(hass, frozen, delta):
    frozen.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


def minutes(n):
    return timedelta(minutes=n)


async def short_run(hass, frozen, run=minutes(3)):
    """The surplus arrives, the element starts on it, the surplus leaves."""
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass), "test premise"
    await tick(hass, frozen, run)
    await set_state(hass, PV_EXCESS, STATE_OFF)
    assert not switch_on(hass)


# --- the rule -------------------------------------------------------------------


async def test_normal_heating_that_lost_its_surplus_at_once_waits(hass, world):  # noqa: F811
    """No boost is configured at all: this is the eco gate's own heat."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)  # tank at 40, target 50, parked
        await short_run(hass, frozen)
        assert locked_until(hass) == (dt_util.utcnow() + SURPLUS_BOOST_LOCKOUT).isoformat()
        assert heater(hass).attributes["smart_eco_state"] == "Blocked by eco condition"

        # The surplus is back a minute later. It does not count.
        await tick(hass, frozen, minutes(1))
        world["turn_on"].clear()
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert not switch_on(hass)
        assert world["turn_on"] == []
        attributes = heater(hass).attributes
        assert heater(hass).state == STATE_OFF, "parked by the gate"
        assert attributes["surplus_active"] is True, "the live reading"
        assert attributes["smart_eco_condition_met"] is False
        assert attributes["smart_eco_state"] == WAITING
        assert attributes["eco_gated"] is False

        # When the lockout ends the surplus counts again, with no new event.
        await tick(hass, frozen, SURPLUS_BOOST_LOCKOUT - minutes(1))
        assert switch_on(hass)
        assert locked_until(hass) is None
        assert heater(hass).state == STATE_ELECTRIC
        assert heater(hass).attributes["smart_eco_condition_met"] is True
        assert heater(hass).attributes["smart_eco_state"] == "Heating in eco"


async def test_the_flapping_of_2026_11_18_costs_one_run_not_six(hass, world):  # noqa: F811
    """Three minutes on, about a minute off, six times, as recorded."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        world["turn_on"].clear()
        world["turn_off"].clear()
        for _ in range(6):
            await set_state(hass, PV_EXCESS, STATE_ON)
            await tick(hass, frozen, timedelta(seconds=190))
            await set_state(hass, PV_EXCESS, STATE_OFF)
            await tick(hass, frozen, timedelta(seconds=66))
        assert len(world["turn_on"]) == 1
        assert len(world["turn_off"]) == 1


# The surplus entity on 2026-11-18, UTC, as recorded: (time, state).
TRACE = [
    ("10:34:53", STATE_ON),
    ("11:15:42", STATE_OFF),
    ("11:16:48", STATE_ON),
    ("11:47:09", STATE_OFF),
    ("12:11:35", STATE_ON),
    ("12:14:43", STATE_OFF),
    ("12:15:49", STATE_ON),
    ("12:19:02", STATE_OFF),
    ("12:20:08", STATE_ON),
    ("12:23:19", STATE_OFF),
    ("12:24:33", STATE_ON),
    ("12:27:47", STATE_OFF),
    ("12:48:37", STATE_ON),
    ("12:51:45", STATE_OFF),
    ("12:52:56", STATE_ON),
    ("12:56:05", STATE_OFF),
]


async def test_the_recorded_trace_costs_seven_relay_operations_not_fifteen(hass, world):  # noqa: F811
    """The whole afternoon, replayed, for a tank below its normal target throughout.

    Counted from the first fall, as the incident was: until 4.6.7 the relay
    followed all 15 changes. The two long runs are untouched (40 and 30
    minutes), the first 3-minute run locks out until 12:44:43, the flapping
    inside it is ignored, and the next 3-minute run at 12:48 locks out again.
    The recorded surplus is replayed as it was; with the element held off it
    would not have flapped at all.
    """

    def at(clock):
        return datetime.fromisoformat(f"2026-11-18T{clock}+00:00")

    async def advance(until):
        """Move the clock in steps; a tank that is being heated warms."""
        while dt_util.utcnow() < until:
            frozen.move_to(min(until, dt_util.utcnow() + minutes(10)))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
            if switch_on(hass):
                warmer = float(hass.states.get(UPSTAIRS_SENSOR).state) + 0.5
                hass.states.async_set(UPSTAIRS_SENSOR, f"{warmer:.1f}")
                await hass.async_block_till_done()

    with freeze_time(at("10:30:00")) as frozen:
        await setup_inputs(hass, temperature="30")
        operations = []
        for clock, state in TRACE:
            await advance(at(clock))
            before = switch_on(hass)
            await set_state(hass, PV_EXCESS, state)
            if switch_on(hass) != before:
                operations.append((clock, "on" if switch_on(hass) else "off"))
        assert operations == [
            ("10:34:53", "on"),
            ("11:15:42", "off"),
            ("11:16:48", "on"),
            ("11:47:09", "off"),
            ("12:11:35", "on"),
            ("12:14:43", "off"),
            ("12:48:37", "on"),
            ("12:51:45", "off"),
        ]
        assert len(world["turn_on"]) == 4 and len(world["turn_off"]) == 4


async def test_a_long_run_is_not_locked_out(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await short_run(hass, frozen, run=SURPLUS_BOOST_SHORT_RUN)
        assert locked_until(hass) is None
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)


async def test_a_lockout_that_ends_with_no_surplus_starts_nothing(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await short_run(hass, frozen)
        world["turn_on"].clear()
        await tick(hass, frozen, SURPLUS_BOOST_LOCKOUT)
        assert locked_until(hass) is None
        assert not switch_on(hass)
        assert world["turn_on"] == []
        assert heater(hass).attributes["smart_eco_state"] == "Blocked by eco condition"
        # And the next surplus is an ordinary one.
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)


async def test_a_tank_that_was_not_heating_starts_no_lockout(hass, world):  # noqa: F811
    """Satisfied, element off: its draw did not clear the surplus."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, temperature="52")
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert not switch_on(hass), "test premise"
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is None


async def test_performance_under_the_gate_is_locked_out_like_electric(hass, world):  # noqa: F811
    """The gate parks and restores performance too (a cycle waiting for eco)."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        entities(hass)["Upstairs"]._smart_eco_last_heating_mode = STATE_PERFORMANCE
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert heater(hass).state == STATE_PERFORMANCE, "test premise"
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is not None
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert not switch_on(hass)


# --- heat that does not hang on the surplus ------------------------------------------


async def test_the_utility_is_never_locked_out(hass, world):  # noqa: F811
    """Grid power that comes and goes on a schedule is nobody's flapping."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await short_run(hass, frozen)
        assert locked_until(hass) is not None

        await set_state(hass, SOURCE, "Utility")
        assert switch_on(hass)
        assert heater(hass).attributes["smart_eco_condition_met"] is True
        await set_state(hass, SOURCE, "Battery")
        assert not switch_on(hass)


async def test_a_template_is_never_locked_out(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        hass.states.async_set("input_boolean.allow", STATE_OFF)
        await setup_inputs(hass, template="{{ is_state('input_boolean.allow', 'on') }}")
        await short_run(hass, frozen)
        assert locked_until(hass) is not None
        await set_state(hass, "input_boolean.allow", STATE_ON)
        assert switch_on(hass)


async def test_heat_the_utility_also_allows_starts_no_lockout(hass, world):  # noqa: F811
    """It goes on when the surplus leaves, so the surplus leaving proves nothing."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, source="Utility")
        assert switch_on(hass), "test premise: heating on the utility"
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert switch_on(hass)
        assert locked_until(hass) is None


async def test_heat_a_template_also_allows_starts_no_lockout(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, template="{{ true }}")
        assert switch_on(hass), "test premise"
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert switch_on(hass)
        assert locked_until(hass) is None


async def test_the_utility_arriving_after_the_surplus_fell_changes_no_verdict(hass, world):  # noqa: F811
    """A known cost, pinned so it is not mistaken for a guarantee.

    On a changeover the surplus falls and the utility arrives a moment apart.
    If the surplus falls first, the run was short and the lockout is set; the
    heat goes on, on the utility. When the utility then leaves inside the
    lockout, a surplus that is back does not count yet. Each entity changes in
    its own event, so which comes first is not this integration's to choose.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        await set_state(hass, SOURCE, "Utility")
        assert switch_on(hass)
        assert locked_until(hass) is not None

        await tick(hass, frozen, minutes(10))
        await set_state(hass, PV_EXCESS, STATE_ON)
        await set_state(hass, SOURCE, "Battery")
        assert not switch_on(hass)
        await tick(hass, frozen, SURPLUS_BOOST_LOCKOUT)
        assert switch_on(hass)


# --- what is not the surplus leaving ---------------------------------------------------


@pytest.mark.parametrize("word", [STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_a_surplus_entity_that_drops_out_has_not_left(hass, world, word):  # noqa: F811
    """It stops the heat, as it always has. It is not a verdict on the surplus."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, word)
        assert not switch_on(hass)
        assert locked_until(hass) is None
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)


async def test_a_surplus_entity_that_is_removed_has_not_left(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(3))
        hass.states.async_remove(PV_EXCESS)
        await hass.async_block_till_done()
        assert not switch_on(hass)
        assert locked_until(hass) is None


async def test_the_first_reading_after_a_load_is_not_the_surplus_arriving(hass, world):  # noqa: F811
    """A reload finds the surplus on. It may have been on for hours."""
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await setup_inputs(hass, excess=STATE_ON)
        assert switch_on(hass), "test premise"
        await tick(hass, frozen, minutes(5))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert not switch_on(hass)
        assert locked_until(hass) is None, "a run of unknown length was called short"
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)


async def test_a_run_seen_starting_after_a_load_is_still_measured(hass, world):  # noqa: F811
    """The window's start is unknown; the element's own start is not."""
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, excess=STATE_ON, temperature="52")
        assert not switch_on(hass), "test premise: satisfied"
        await tick(hass, frozen, minutes(60))
        await set_state(hass, UPSTAIRS_SENSOR, "45")  # a draw
        assert switch_on(hass)
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is not None


async def test_a_switch_report_that_is_not_a_start_dates_no_run(hass, world):  # noqa: F811
    """After a load, an attribute update from a relay on for hours is not a start."""
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await setup_inputs(hass, excess=STATE_ON)
        await tick(hass, frozen, minutes(20))
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON, {"rssi": -61})
        await hass.async_block_till_done()
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is None


async def test_a_running_lockout_is_not_renewed_by_the_flapping_it_sits_out(hass, world):  # noqa: F811
    """With min_on_duration the parked element stays on for a while.

    The surplus returning and leaving again in that time must not start the
    lockout over.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, min_on_duration={"minutes": 5})
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)
        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        first = locked_until(hass)
        assert first is not None
        assert switch_on(hass), "test premise: the OFF is held back by min_on"

        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) == first
        # Let the held OFF land, so no timer outlives the test.
        await tick(hass, frozen, minutes(5))
        assert not switch_on(hass)


# --- restarts -----------------------------------------------------------------------

HEATING = {
    "temperature": 50,
    "smart_eco_mode": SMART_ECO_MODE_AUTO_RESUME,
    "smart_eco_last_heating_mode": "electric",
    "surplus_boost_enabled": True,
    "surplus_boost_active": True,
    "surplus_active": True,
}


async def test_nothing_is_locked_out_inside_the_startup_grace(hass, world):  # noqa: F811
    """A surplus entity that flickers as it comes up is not a short run.

    Until 4.6.7 the lockout this set cost a boost. Now it would park the tank
    when the grace ends: a restart switching off an element that was heating
    before it, on real surplus.
    """
    with freeze_time(T0) as frozen:
        await restarted(hass, restored=HEATING, tank="40")
        assert switch_on(hass), "test premise"
        world["turn_off"].clear()

        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        await tick(hass, frozen, timedelta(seconds=5))
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        assert locked_until(hass) is None

        await tick(hass, frozen, timedelta(seconds=55))
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        await finish_starting(hass)
        await tick(hass, frozen, STARTUP_GRACE)
        assert switch_on(hass)
        assert world["turn_off"] == [], "the restart operated the relay"


async def test_a_surplus_that_came_up_in_the_grace_has_no_known_start(hass, world):  # noqa: F811
    """Off for a minute after a restart, then on: that is the sensor, not the sun.

    The element had been on for an hour. A real loss eight minutes after the
    restart parks it, and is not a short run.
    """
    with freeze_time(T0) as frozen:
        await restarted(hass, restored=HEATING, tank="40", surplus=STATE_OFF)
        await finish_starting(hass)
        await tick(hass, frozen, minutes(1))
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        # The relay replays states as it reconnects, with no command behind
        # them (2026-10-02: on then off within 250 ms). Not a run beginning.
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        await tick(hass, frozen, STARTUP_GRACE)
        assert switch_on(hass), "test premise"

        await tick(hass, frozen, minutes(4))
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        assert not switch_on(hass)
        assert locked_until(hass) is None
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert switch_on(hass)


async def test_a_restored_lockout_still_holds_the_gate(hass, world):  # noqa: F811
    """Parked before the restart for it, parked after: nothing is operated."""
    with freeze_time(T0) as frozen:
        mock_restore_cache(
            hass,
            (
                State(
                    UPSTAIRS,
                    STATE_OFF,
                    {
                        "temperature": 50,
                        "smart_eco_mode": SMART_ECO_MODE_AUTO_RESUME,
                        "smart_eco_last_heating_mode": "electric",
                        "surplus_boost_locked_until": (T0 + minutes(20)).isoformat(),
                    },
                ),
            ),
        )
        await setup_inputs(hass, excess=STATE_ON)
        assert not switch_on(hass)
        assert world["turn_on"] == []
        assert heater(hass).attributes["smart_eco_state"] == WAITING

        await tick(hass, frozen, minutes(20))
        assert switch_on(hass)
        assert locked_until(hass) is None


async def test_a_restored_lockout_is_never_longer_than_a_lockout(hass, world):  # noqa: F811
    """A stamp from a clock that jumped would park normal heating for as long as it said."""
    with freeze_time(T0) as frozen:
        mock_restore_cache(
            hass,
            (
                State(
                    UPSTAIRS,
                    STATE_OFF,
                    {
                        "temperature": 50,
                        "smart_eco_mode": SMART_ECO_MODE_AUTO_RESUME,
                        "surplus_boost_locked_until": (T0 + timedelta(days=3)).isoformat(),
                    },
                ),
            ),
        )
        await setup_inputs(hass, excess=STATE_ON)
        assert locked_until(hass) == (T0 + SURPLUS_BOOST_LOCKOUT).isoformat()
        await tick(hass, frozen, SURPLUS_BOOST_LOCKOUT)
        assert switch_on(hass)
