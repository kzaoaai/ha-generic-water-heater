"""The integration on a Fahrenheit install."""

from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    auto_enable_custom_integrations,
    setup_both,
    world,
)

RATE = "sensor.upstairs_heat_up_rate"


async def test_the_heat_up_rate_is_labelled_in_fahrenheit_on_a_fahrenheit_system(hass, world):
    """The rate comes from raw readings in the system unit, but was always labelled C per hour."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await setup_both(hass)

    assert hass.states.get(RATE).attributes["unit_of_measurement"] == "°F/h"


async def test_the_heat_up_rate_stays_in_celsius_on_a_metric_system(hass, world):
    """Guard: the metric label is unchanged."""
    hass.config.units = METRIC_SYSTEM
    await setup_both(hass)

    assert hass.states.get(RATE).attributes["unit_of_measurement"] == "°C/h"
