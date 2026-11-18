"""The temperature failsafe: no reading, no heat -- in every heating mode.

Before 4.6.1 the failsafe was one turn_off at the moment the sensor reported
unavailable, followed by a control pass. In PERFORMANCE that pass switched the
element straight back on (performance needs no temperature to decide), and a
disinfection cycle runs in PERFORMANCE. In ELECTRIC an OFF held back by
min_on_duration was retried through a pass that skipped control for want of a
temperature, so it was never sent. The hold now lives in the control pass.
"""

from datetime import timedelta

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, State
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.generic_water_heater.water_heater_const import (
    FIRST_READING_WITHIN,
)

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    commanded,
    entities,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"


async def heating_in(hass, mode, **entry_kwargs):
    """Both entries loaded, PV excess on, the upstairs tank heating in `mode`."""
    await setup_both(hass, stagger_seconds=0.0, **entry_kwargs)
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    if mode != STATE_ELECTRIC:
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": UPSTAIRS, "operation_mode": mode},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON, "test premise"
    return entities(hass)["Upstairs"]


async def test_performance_is_held_off_while_the_reading_is_missing(hass, world):
    upstairs = await heating_in(hass, STATE_PERFORMANCE)
    world["turn_on"].clear()

    hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
    assert UPSTAIRS_SWITCH not in commanded(world["turn_on"]), (
        "performance switched the element straight back on with no reading"
    )
    # The mode is the person's; only the switch is held.
    assert upstairs.state == STATE_PERFORMANCE


async def test_the_failsafe_is_not_deferred_by_min_on(hass, world):
    """A held OFF used to be retried through the missing-temperature skip."""
    with freeze_time(TRIGGER) as frozen:
        await heating_in(hass, STATE_ELECTRIC, min_on_duration={"seconds": 300})

        hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNKNOWN)
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF, (
            "min_on_duration held the failsafe back"
        )

        # And it stays off through the retry window.
        frozen.tick(timedelta(seconds=400))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_a_relay_that_reappears_on_without_a_reading_is_switched_off(hass, world):
    """A combined relay/probe device drops off: both entities go, both return."""
    await heating_in(hass, STATE_ELECTRIC)
    hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF

    hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_heat_resumes_when_the_reading_returns(hass, world):
    upstairs = await heating_in(hass, STATE_PERFORMANCE)
    hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF

    hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON
    assert upstairs.state == STATE_PERFORMANCE


async def test_setup_before_the_first_reading_does_not_cycle_the_relay(hass, world):
    """No reading YET is not a lost reading.

    Setup runs a control pass before it reads the sensor, and on a restart the
    sensor's integration may load after this one. Treating that as the
    failsafe switched a heating element off and on again on every options
    save, and walked its stagger anchor forward.
    """
    live = await heating_in(hass, STATE_ELECTRIC)
    entry = next(
        entry
        for entry in hass.config_entries.async_entries("generic_water_heater")
        if entry.entry_id == live._entry_id
    )
    world["turn_off"].clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert UPSTAIRS_SWITCH not in commanded(world["turn_off"])
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


# "nan" and "inf" parse as floats, "error" and "" do not. Home Assistant refuses
# all of them from a temperature sensor entity, so they need a source such as a
# template or an input_text. None of them is a reading.
NO_READING_STATES = ["nan", "inf", "-inf", "error", ""]


@pytest.mark.parametrize("bad", NO_READING_STATES)
@pytest.mark.parametrize("mode", [STATE_ELECTRIC, STATE_PERFORMANCE])
async def test_a_state_that_is_not_a_number_is_a_missing_reading(hass, world, mode, bad):
    """It used to leave an element that was on running, with no reading behind it.

    A non-numeric state was dropped by the heater and kept the last temperature;
    "nan" became the temperature, and every comparison with it is false, so
    control kept whatever the element was doing. "-inf" would even have
    switched it on.
    """
    with freeze_time(TRIGGER):
        await heating_in(hass, mode, min_on_duration={"seconds": 300})
        world["turn_on"].clear()

        hass.states.async_set(UPSTAIRS_SENSOR, bad, {"device_class": "temperature"})
        await hass.async_block_till_done()

        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF, (
            "the element stayed on with no usable reading"
        )
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])
        assert entities(hass)["Upstairs"]._temperature_lost

        hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON
        assert not entities(hass)["Upstairs"]._temperature_lost


@pytest.mark.parametrize("bad", ["nan", "error", ""])
async def test_the_heater_itself_never_ignores_a_reading_it_cannot_use(hass, world, bad):
    """Its handler used to return on a value it could not parse.

    The tracking helper no longer delivers one, but the failsafe must not
    depend on that: leaving the element as it was is the unsafe answer.
    """
    upstairs = await heating_in(hass, STATE_ELECTRIC)

    await upstairs._async_sensor_changed(bad, dt_util.utcnow(), {})
    await hass.async_block_till_done()

    assert upstairs._temperature_lost
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


@pytest.mark.parametrize("bad", ["nan", "-inf", "error"])
async def test_a_tank_state_that_is_not_a_number_at_startup_is_no_reading_yet(
    hass, world, bad
):
    """float() raised on "error" and failed the heater's setup; "nan" was kept.

    At startup that is treated like unknown: no temperature, and not the
    failsafe, because the source may simply not have a value yet (see
    test_setup_before_the_first_reading_does_not_cycle_the_relay).
    """
    hass.states.async_set(UPSTAIRS_SENSOR, bad, {"device_class": "temperature"})
    await setup_both(hass, stagger_seconds=0.0)

    upstairs = entities(hass)["Upstairs"]
    assert hass.states.get(UPSTAIRS) is not None, "the heater did not load"
    assert upstairs.current_temperature is None
    assert not upstairs._temperature_lost

    hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
    await hass.async_block_till_done()
    assert upstairs.current_temperature == 41.0


# --- no first reading ---------------------------------------------------------


async def loaded_with_no_reading(hass, sensor_state, mode=STATE_ELECTRIC):
    """Both entries loaded with the upstairs element already on and no reading.

    A restart mid-heat: the relay kept its state, the tank sensor has nothing.
    """
    hass.states.async_set(PV_EXCESS, STATE_ON)
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    if sensor_state is None:
        hass.states.async_remove(UPSTAIRS_SENSOR)
    else:
        hass.states.async_set(UPSTAIRS_SENSOR, sensor_state, {"device_class": "temperature"})
    mock_restore_cache(hass, (State(UPSTAIRS, mode, {"temperature": 60}),))
    await setup_both(hass, stagger_seconds=0.0)
    upstairs = entities(hass)["Upstairs"]
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON, "test premise: found on"
    assert not upstairs._temperature_lost, "test premise: no reading yet is not lost"
    return upstairs


async def after(hass, start, delay):
    async_fire_time_changed(hass, start + delay)
    await hass.async_block_till_done()


@pytest.mark.parametrize("sensor_state", [STATE_UNKNOWN, STATE_UNAVAILABLE, "nan", None])
@pytest.mark.parametrize("mode", [STATE_ELECTRIC, STATE_PERFORMANCE])
async def test_an_element_found_on_with_no_reading_is_stopped_after_the_wait(
    hass, world, mode, sensor_state
):
    """"No reading yet" used to last for as long as the sensor said nothing new.

    It is deliberately not the failsafe, so the control pass skipped, and an
    element found on at a restart ran on the tank's thermostat alone until the
    sensor's state next changed. A sensor that is gone never changes.
    """
    with freeze_time(TRIGGER) as frozen:
        upstairs = await loaded_with_no_reading(hass, sensor_state, mode)
        world["turn_off"].clear()

        frozen.tick(FIRST_READING_WITHIN - timedelta(seconds=1))
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"]), "stopped too early"
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON

        frozen.tick(timedelta(seconds=2))
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert upstairs._temperature_lost
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF

        # And it ends the way every lost reading ends: with a reading.
        hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert not upstairs._temperature_lost
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


def test_the_wait_is_longer_than_a_routine_start_and_short_enough_to_matter():
    """Its tests use the constant by name, so only this pins its size.

    A real restart leaves the sensor silent for about a minute. Below that the
    wait would stop a healthy element at every start; at ten minutes it would
    be the Repairs delay, with an unwatched element drawing all the while.
    """
    assert timedelta(minutes=1) < FIRST_READING_WITHIN <= timedelta(minutes=3)


async def test_a_first_reading_inside_the_wait_ends_it(hass, world):
    """The routine case: the sensor's integration loads a moment later."""
    with freeze_time(TRIGGER) as frozen:
        upstairs = await loaded_with_no_reading(hass, STATE_UNKNOWN)
        assert upstairs._first_reading_timer is not None
        world["turn_off"].clear()

        frozen.tick(timedelta(seconds=60))
        hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert upstairs._first_reading_timer is None

        frozen.tick(FIRST_READING_WITHIN)
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert not upstairs._temperature_lost
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"])
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


async def test_a_heater_that_loads_with_a_reading_starts_no_wait(hass, world):
    """Guard: an options save reloads with the sensor already there."""
    await heating_in(hass, STATE_ELECTRIC)

    assert entities(hass)["Upstairs"]._first_reading_timer is None


async def test_on_a_home_assistant_start_the_wait_begins_once_it_has_started(hass, world):
    """A slow start is not the sensor's silence.

    The heater can load minutes before the sensor's integration does. Counting
    from its own load would stop the element on every slow start.
    """
    with freeze_time(TRIGGER) as frozen:
        hass.set_state(CoreState.starting)
        upstairs = await loaded_with_no_reading(hass, STATE_UNKNOWN)
        world["turn_off"].clear()

        frozen.tick(FIRST_READING_WITHIN * 3)
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert not upstairs._temperature_lost
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"])

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        frozen.tick(FIRST_READING_WITHIN - timedelta(seconds=1))
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert not upstairs._temperature_lost

        frozen.tick(timedelta(seconds=2))
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert upstairs._temperature_lost
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_unloading_during_the_wait_cancels_it(hass, world):
    """A timer left armed would call back into a removed entity."""
    with freeze_time(TRIGGER) as frozen:
        upstairs = await loaded_with_no_reading(hass, STATE_UNKNOWN)
        entry = hass.config_entries.async_get_entry(upstairs._entry_id)
        world["turn_off"].clear()

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert upstairs._first_reading_timer is None

        frozen.tick(FIRST_READING_WITHIN * 2)
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"])


async def test_unloading_before_the_start_finishes_arms_nothing(hass, world):
    """The wait is registered for the end of the start; a removed heater must not keep it.

    Left registered, the listener armed the timer on an entity that was gone,
    and two minutes later it switched off a relay it no longer owned.
    """
    with freeze_time(TRIGGER) as frozen:
        hass.set_state(CoreState.starting)
        upstairs = await loaded_with_no_reading(hass, STATE_UNKNOWN)
        entry = hass.config_entries.async_get_entry(upstairs._entry_id)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        world["turn_off"].clear()

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert upstairs._first_reading_timer is None

        frozen.tick(FIRST_READING_WITHIN * 2)
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"])


async def test_a_sensor_that_speaks_during_the_start_needs_no_wait(hass, world):
    """Its first word came before the wait could begin, so there is nothing to wait for."""
    with freeze_time(TRIGGER) as frozen:
        hass.set_state(CoreState.starting)
        upstairs = await loaded_with_no_reading(hass, STATE_UNKNOWN)
        hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
        await hass.async_block_till_done()
        world["turn_off"].clear()

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert upstairs._first_reading_timer is None

        frozen.tick(FIRST_READING_WITHIN * 2)
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert not upstairs._temperature_lost
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"])


async def test_performance_does_not_start_heat_before_any_reading(hass, world):
    """It needs no temperature to decide, so it switched the element on at once.

    With no reading at load that was heat with nothing behind it, and the wait
    then had to stop it: an element found off went on, then off again.
    """
    with freeze_time(TRIGGER) as frozen:
        hass.states.async_set(PV_EXCESS, STATE_ON)
        hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNKNOWN, {"device_class": "temperature"})
        mock_restore_cache(hass, (State(UPSTAIRS, STATE_PERFORMANCE, {"temperature": 60}),))
        await setup_both(hass, stagger_seconds=0.0)
        upstairs = entities(hass)["Upstairs"]
        assert upstairs.state == STATE_PERFORMANCE, "test premise"

        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])
        frozen.tick(FIRST_READING_WITHIN + timedelta(seconds=1))
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert upstairs._temperature_lost
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF

        # The first reading is all it was waiting for.
        hass.states.async_set(UPSTAIRS_SENSOR, "41", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


async def test_a_reload_during_an_outage_does_not_bring_performance_back(hass, world):
    """A reloaded heater starts with no memory of the lost reading.

    Performance then switched the element back on, with the sensor still down,
    until something set the failsafe again: nothing did.
    """
    with freeze_time(TRIGGER) as frozen:
        upstairs = await heating_in(hass, STATE_PERFORMANCE)
        hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF, "test premise"
        world["turn_on"].clear()

        assert await hass.config_entries.async_reload(upstairs._entry_id)
        await hass.async_block_till_done()
        assert entities(hass)["Upstairs"].state == STATE_PERFORMANCE, "test premise"
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])

        frozen.tick(FIRST_READING_WITHIN + timedelta(seconds=1))
        await after(hass, dt_util.utcnow(), timedelta(0))
        assert entities(hass)["Upstairs"]._temperature_lost
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
