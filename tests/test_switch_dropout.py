"""A short relay dropout no longer flashes the heater unavailable."""

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    TRIGGER,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"


async def tick(hass, frozen, seconds):
    frozen.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_a_blip_under_two_minutes_keeps_the_heater_available(hass, world):
    with freeze_time(TRIGGER) as frozen:
        await setup_both(hass)
        assert hass.states.get(UPSTAIRS).state != STATE_UNAVAILABLE, "premise"
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS).state != STATE_UNAVAILABLE
        await tick(hass, frozen, 90)
        assert hass.states.get(UPSTAIRS).state != STATE_UNAVAILABLE
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        await tick(hass, frozen, 120)
        assert hass.states.get(UPSTAIRS).state != STATE_UNAVAILABLE


async def test_a_lasting_dropout_shows_unavailable_and_recovers(hass, world):
    with freeze_time(TRIGGER) as frozen:
        await setup_both(hass)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await tick(hass, frozen, 121)
        assert hass.states.get(UPSTAIRS).state == STATE_UNAVAILABLE
        # A second unavailable report must not restart anything.
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE, {"rssi": -80})
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS).state == STATE_UNAVAILABLE

        hass.states.async_set(UPSTAIRS_SWITCH, "off")
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS).state != STATE_UNAVAILABLE
