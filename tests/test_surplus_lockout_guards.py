"""Guards of the surplus lockout that nothing pinned at first.

Each test names a mutation that left the rest of the suite green: a reviewer
removed the guard on a copy, the whole suite passed, and the test below is
what went red. The behaviour itself is in test_surplus_lockout.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

from custom_components.generic_water_heater.water_heater import (
    STARTUP_GRACE,
    SURPLUS_BOOST_LOCKOUT,
)
from tests.test_eco_inputs import (
    BOOST,
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
from tests.test_restart_boost import BOOSTING, finish_starting, restarted, say
from tests.test_surplus_boost import boosting
from tests.test_surplus_lockout import locked_until, minutes, short_run, tick, WAITING

T0 = datetime(2026, 11, 18, 12, 11, 35, tzinfo=timezone.utc)


async def test_an_element_held_on_by_a_manual_pause_is_not_the_surplus_carrying_it(
    hass, world  # noqa: F811
):
    """Mutant: _surplus_heating_now does not ask whether Smart Eco is enforcing.

    A person switched the element on over a parked tank: Smart Eco is paused,
    the heat hangs on nothing of the surplus's. A surplus that comes and goes
    meanwhile has not failed to carry it, and must not park the tank for 30
    minutes once the pause ends.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        assert switch_on(hass)
        assert heater(hass).attributes["smart_eco_pause_reason"], "test premise: paused"
        await set_state(hass, PV_EXCESS, STATE_ON)
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is None


async def test_a_person_choosing_off_over_a_held_element_is_not_the_surplus_leaving(
    hass, world  # noqa: F811
):
    """A person's OFF pauses the policy, so what min_on holds on is not the surplus's.

    Written against a mode check that _surplus_heating_now no longer has: the
    pause already makes the policy not enforcing, which is what it asks.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, min_on_duration={"minutes": 5})
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)
        await tick(hass, frozen, minutes(1))
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": UPSTAIRS, "operation_mode": STATE_OFF},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert switch_on(hass), "test premise: held on by min_on_duration"
        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is None
        await tick(hass, frozen, minutes(5))


async def test_a_utility_that_may_not_be_used_does_not_excuse_the_surplus(
    hass, world  # noqa: F811
):
    """Mutant: the utility term of _surplus_heating_now ignores "Eco: Use Utility".

    The utility entity says Utility but the owner switched its use off: it
    does not allow heat, so the heat hangs on the surplus and a short run is
    short.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass, source="Utility")
        assert switch_on(hass), "test premise: on the utility"
        await entities(hass)["Upstairs"].async_set_eco_use_utility(False)
        await hass.async_block_till_done()
        assert not switch_on(hass)
        await short_run(hass, frozen)
        assert locked_until(hass) is not None


async def test_a_run_that_stops_and_starts_again_is_dated_from_its_second_start(
    hass, world  # noqa: F811
):
    """Mutant: _note_boost_heating does not clear the stamp when the element stops.

    The surplus stays up for 15 minutes; the tank is satisfied for most of it
    and a draw starts the element a minute before the surplus goes. That last
    run is short and the surplus did not carry it.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)
        await tick(hass, frozen, minutes(1))
        await set_state(hass, UPSTAIRS_SENSOR, "45")
        await set_state(hass, UPSTAIRS_SENSOR, "51")
        assert not switch_on(hass), "test premise: satisfied"
        await tick(hass, frozen, minutes(14))
        await set_state(hass, UPSTAIRS_SENSOR, "46")
        assert switch_on(hass), "test premise: a draw"
        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is not None


async def test_the_end_of_a_lockout_on_a_satisfied_tank_is_published(hass, world):  # noqa: F811
    """Mutants: the lockout's end does not refresh the label, or does not write state.

    The gate opens and there is nothing to heat, so no relay event follows and
    nothing else writes the state. The card must stop saying the surplus is
    being waited out.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await short_run(hass, frozen)
        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert heater(hass).attributes["smart_eco_state"] == WAITING
        await set_state(hass, UPSTAIRS_SENSOR, "45")
        await set_state(hass, UPSTAIRS_SENSOR, "51")
        await tick(hass, frozen, minutes(5))  # the spike filter lets it through
        assert heater(hass).attributes["smart_eco_state"] == WAITING
        world["turn_on"].clear()
        await tick(hass, frozen, SURPLUS_BOOST_LOCKOUT)
        assert world["turn_on"] == []
        assert locked_until(hass) is None
        assert heater(hass).attributes["smart_eco_state"] != WAITING


async def test_a_surplus_that_arrives_under_a_running_element_is_a_seen_rise(hass, world):  # noqa: F811
    """Mutant: the first read of the eco entities is never marked as read.

    Every rise then looks like the first reading after a load and its window
    start is unknown. Here the element is already heating on the utility when
    the surplus arrives and the boost starts on it: no relay event dates the
    run, only the rise does.
    """
    with freeze_time(T0) as frozen:
        await boosting(hass, temperature="40", source="Utility")
        assert switch_on(hass), "test premise: heating on the utility"
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert heater(hass).attributes["surplus_boost_active"] is True
        await tick(hass, frozen, minutes(3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert locked_until(hass) is not None


async def test_a_relay_that_comes_up_on_in_the_grace_dates_no_run(hass, world):  # noqa: F811
    """Mutant: a relay going OFF to ON inside the startup grace stamps a run start.

    After a restart relays replay states with no command behind them. A stamp
    made then dates a run of unknown length as seconds old, and the surplus
    going a few minutes later is called a short run.
    """
    with freeze_time(T0) as frozen:
        await restarted(
            hass, restored=BOOSTING, surplus=STATE_ON, tank="40", relay_found=STATE_OFF
        )
        await finish_starting(hass)
        await hass.async_block_till_done()
        await tick(hass, frozen, STARTUP_GRACE + timedelta(seconds=30))
        assert heater(hass).attributes["surplus_active"] is True
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        assert locked_until(hass) is None


async def test_a_switch_that_reappears_is_not_a_crash(hass, world, caplog):  # noqa: F811
    """Mutant: the run-start check reads old_state without asking whether there is one.

    The relay entity is removed and comes back: its first event has no old
    state. The handler has to get to the end and write the state.
    """
    with freeze_time(T0):
        await setup_inputs(hass, excess=STATE_ON)
        assert switch_on(hass)
        hass.states.async_remove(UPSTAIRS_SWITCH)
        await hass.async_block_till_done()
        assert heater(hass).state == STATE_UNAVAILABLE
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        assert heater(hass).state != STATE_UNAVAILABLE
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_switch_that_is_removed_while_off_is_not_a_crash(hass, world, caplog):  # noqa: F811
    """Mutant: the run-start check reads new_state without asking whether there is one."""
    with freeze_time(T0):
        await setup_inputs(hass, excess=STATE_ON, temperature="52")
        assert not switch_on(hass)
        hass.states.async_remove(UPSTAIRS_SWITCH)
        await hass.async_block_till_done()
        assert heater(hass).state == STATE_UNAVAILABLE
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_the_label_does_not_say_waiting_once_the_utility_allows_heat(hass, world):  # noqa: F811
    """Mutant: the "Waiting after a short surplus" label ignores the eco condition.

    The surplus reads active and is locked out, and then the utility arrives:
    the condition is met, the element heats, and the card must say so.
    """
    with freeze_time(T0) as frozen:
        await setup_inputs(hass)
        await short_run(hass, frozen)
        await tick(hass, frozen, minutes(1))
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert heater(hass).attributes["smart_eco_state"] == WAITING
        await set_state(hass, SOURCE, "Utility")
        assert switch_on(hass)
        assert heater(hass).attributes["smart_eco_state"] == "Heating in eco"
