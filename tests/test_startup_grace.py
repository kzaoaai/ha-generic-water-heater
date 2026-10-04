"""A Home Assistant start must not be read as the eco condition going false.

The 2026-10-02 restart, replayed: both tanks heating under a met eco condition.
At startup the eco template rendered false because its source entities had not
loaded, so the gate parked both tanks and switched the elements off. The PV
excess sensor then reported "off" for ~70 s (a delay_on) before going "on".
Upstairs also saw its relay report on then off within 250 ms with no command
behind either, and took the ON for a person -- starting a 3-hour eco pause.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_OFF, STATE_ON
from homeassistant.core import CoreState, State
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.generic_water_heater import SMART_ECO_MODE_AUTO_RESUME
from custom_components.generic_water_heater.water_heater import STARTUP_GRACE
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

UPSTAIRS = "water_heater.upstairs"
START = datetime(2026, 10, 2, 10, 57, 35, tzinfo=timezone.utc)


COMMAND_CAP = 20


def cap_switch_commands(hass, world):  # noqa: F811
    """Stop reflecting switch commands after COMMAND_CAP of them.

    The world fixture's switch reports instantly, so a regression that makes
    the override path and the control loop chase each other spins for ever
    instead of failing. Capped, it stops and the assertions below say why.
    """
    from homeassistant.const import STATE_OFF, STATE_ON

    count = {"n": 0}

    def _make(state, key):
        async def _handler(call):
            world[key].append(call)
            count["n"] += 1
            if count["n"] > COMMAND_CAP:
                return
            entity_id = call.data.get("entity_id")
            for eid in [entity_id] if isinstance(entity_id, str) else list(entity_id or []):
                hass.states.async_set(eid, state)
        return _handler

    hass.services.async_register("homeassistant", "turn_on", _make(STATE_ON, "turn_on"))
    hass.services.async_register("homeassistant", "turn_off", _make(STATE_OFF, "turn_off"))
    return count


def heater(hass):
    return hass.states.get(UPSTAIRS)


async def start_heating_tank(hass, *, running: bool = False):
    """Load the entry the way a restart does: restored mid-heat, sources absent."""
    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                "electric",
                {"temperature": 45, "smart_eco_mode": SMART_ECO_MODE_AUTO_RESUME},
            ),
        ),
    )
    hass.states.async_remove(PV_EXCESS)
    hass.states.async_set(UPSTAIRS_SENSOR, "40.7")
    if not running:
        hass.set_state(CoreState.starting)
    entry = build_entry(
        "Upstairs",
        UPSTAIRS_SWITCH,
        UPSTAIRS_SENSOR,
        target_temp=45.0,
        cold_tolerance=2.0,
        hot_tolerance=3.0,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def finish_starting(hass):
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


async def test_a_restart_keeps_the_restored_mode_while_eco_cannot_be_judged(hass, world):  # noqa: F811
    with freeze_time(START) as frozen:
        await start_heating_tank(hass)
        assert heater(hass).state == "electric"
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON
        assert heater(hass).attributes["smart_eco_state"] == "Starting up"

        await finish_starting(hass)
        # The source loads, reading "off" until its delay_on passes.
        frozen.tick(timedelta(seconds=10))
        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        assert heater(hass).state == "electric"
        assert world["turn_off"] == []

        frozen.tick(timedelta(seconds=60))
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert heater(hass).state == "electric"
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON
        assert world["turn_off"] == []


async def test_the_grace_is_bounded_and_eco_then_parks_the_tank(hass, world):  # noqa: F811
    """A source that never comes back must not bypass eco for ever."""
    with freeze_time(START) as frozen:
        await start_heating_tank(hass)
        await finish_starting(hass)

        frozen.tick(STARTUP_GRACE - timedelta(seconds=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert heater(hass).state == "electric"

        frozen.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert heater(hass).state == STATE_OFF
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
        assert heater(hass).attributes["smart_eco_state"] == "Blocked by eco condition"


async def test_the_countdown_starts_when_startup_finishes_not_when_loaded(hass, world):  # noqa: F811
    """Integrations still loading are the whole reason for the window."""
    with freeze_time(START) as frozen:
        await start_heating_tank(hass)
        frozen.tick(STARTUP_GRACE * 2)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert heater(hass).state == "electric"


async def test_a_relay_replaying_states_at_startup_is_not_a_person(hass, world):  # noqa: F811
    with freeze_time(START):
        await start_heating_tank(hass)
        await finish_starting(hass)
        commands = cap_switch_commands(hass, world)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        await hass.async_block_till_done()

        attrs = heater(hass).attributes
        assert commands["n"] < COMMAND_CAP, "override and control loop chased each other"
        assert attrs["smart_eco_pause_reason"] is None
        assert heater(hass).state == "electric"
        # ...and the control loop put the switch back where it should be.
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


async def test_a_flip_after_the_window_is_a_person_again(hass, world):  # noqa: F811
    with freeze_time(START) as frozen:
        await start_heating_tank(hass)
        await finish_starting(hass)
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(STARTUP_GRACE + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        await hass.async_block_till_done()
        assert heater(hass).attributes["smart_eco_pause_reason"] == "manual_off_timer"


async def test_a_reload_while_running_gets_no_grace(hass, world):  # noqa: F811
    """Every source is already loaded; a false condition is a real one."""
    await start_heating_tank(hass, running=True)
    assert heater(hass).state == STATE_OFF
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_a_stale_switch_event_is_not_a_person(hass, world):  # noqa: F811
    """Outside any grace: an event a later state has overtaken is not intent.

    Two writes land before the listener runs, so it sees on->off while the
    switch already reads on again. Judging that against the command baseline
    is how the integration's own turn_off echo was read as a manual OFF.
    """
    with freeze_time(START) as frozen:
        await start_heating_tank(hass)
        await finish_starting(hass)
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(STARTUP_GRACE + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON

        commands = cap_switch_commands(hass, world)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()

        assert commands["n"] < COMMAND_CAP, "override and control loop chased each other"
        assert heater(hass).attributes["smart_eco_pause_reason"] is None
        assert heater(hass).state == "electric"


def delayed_switch(hass, world, latency, lose=()):  # noqa: F811
    """A switch that reports commands back after ``latency`` seconds.

    States in ``lose`` are sent and never reported, like a dropped cloud call.
    """
    from homeassistant.core import callback
    from homeassistant.helpers.event import async_call_later

    sent = []

    def _make(state):
        async def _handler(call):
            sent.append(state)
            if state in lose:
                return
            entity_id = call.data["entity_id"]
            entity_id = entity_id if isinstance(entity_id, str) else entity_id[0]

            @callback
            def _land(_now):
                hass.states.async_set(entity_id, state)

            async_call_later(hass, latency, _land)
        return _handler

    hass.services.async_register("homeassistant", "turn_on", _make(STATE_ON))
    hass.services.async_register("homeassistant", "turn_off", _make(STATE_OFF))
    return sent


async def heating_after_grace(hass, frozen):
    await start_heating_tank(hass)
    await finish_starting(hass)
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    frozen.tick(STARTUP_GRACE + timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def set_mode(hass, mode):
    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": mode},
        blocking=True,
    )


@pytest.mark.parametrize("latency", [0.1, 10])
async def test_our_own_late_echo_is_not_a_person(hass, world, latency):  # noqa: F811
    """Off, then straight back to electric, inside the switch's report delay.

    The OFF we sent lands after the baseline is ON again. Read as a person, it
    put the tank OFF with a manual pause -- against the last thing asked for.
    """
    with freeze_time(START) as frozen:
        await heating_after_grace(hass, frozen)
        sent = delayed_switch(hass, world, latency)
        await set_mode(hass, STATE_OFF)
        await set_mode(hass, "electric")
        await hass.async_block_till_done()
        for _ in range(3):
            frozen.tick(timedelta(seconds=latency))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

        assert heater(hass).state == "electric"
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON
        assert sent == [STATE_OFF, STATE_ON]


async def test_a_command_that_never_reported_cannot_swallow_a_later_flip(hass, world):  # noqa: F811
    """The in-flight record expires; otherwise OFF could become unreachable.

    An OFF we sent is lost, intent returns to ON, and a minute later a person
    turns the switch off at the wall. Matched against the stale OFF it would
    read as our echo and the control loop would turn the element back on.
    """
    with freeze_time(START) as frozen:
        await heating_after_grace(hass, frozen)
        delayed_switch(hass, world, 0.1, lose=(STATE_OFF,))
        await set_mode(hass, STATE_OFF)
        await set_mode(hass, "electric")
        await hass.async_block_till_done()

        frozen.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        await hass.async_block_till_done()

        assert heater(hass).state == STATE_OFF
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
