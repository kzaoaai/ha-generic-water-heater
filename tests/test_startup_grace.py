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
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        await hass.async_block_till_done()

        attrs = heater(hass).attributes
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
