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
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

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
