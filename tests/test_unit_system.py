"""The integration on a Fahrenheit install.

It is built for Celsius. The heater's own control is unit-neutral (target,
tolerances and readings are all in the system's unit), but everything that
compares the tank with a built-in temperature is not converted. The legionella
sensor and the disinfection policy are the part where that is a false safety
claim, so on a Fahrenheit system they do nothing at all.
"""

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.core import State
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.generic_water_heater import (
    LEGIONELLA_MODE_ASAP,
    LEGIONELLA_MODE_OFF,
    LEGIONELLA_MODE_ON,
)
from tests.test_disinfection import SELECT, choose, setup_with_policy
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    TRIGGER,
    UPSTAIRS_SENSOR,
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)
from tests.test_legionella_entities import DAYS_UNTIL, PROGRESS, RISK, START, setup_entry

RATE = "sensor.upstairs_heat_up_rate"
UPSTAIRS = "water_heater.upstairs"
FAHRENHEIT = {"device_class": "temperature", "unit_of_measurement": "°F"}


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


async def test_an_hour_at_61_f_is_not_a_disinfection(hass, world):
    """It was: the hold opens at 60, and nothing converted the reading.

    A tank of cold tap water was recorded as disinfected, the risk read Low and
    the next cycle was put off by a whole interval. On a system that does not
    work in Celsius the sensor now takes no readings and gives no verdict.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    with freeze_time(START) as frozen:
        hass.states.async_set(UPSTAIRS_SENSOR, "58.0", FAHRENHEIT)
        await setup_entry(hass)
        for minute in range(63):
            hass.states.async_set(
                UPSTAIRS_SENSOR, f"{61.0 + (minute % 2) * 0.1:.1f}", FAHRENHEIT
            )
            await hass.async_block_till_done()
            frozen.tick(timedelta(minutes=1))

        risk = hass.states.get(RISK)
        assert risk.state == "Unknown"
        assert risk.attributes["last_disinfection_at"] is None
        assert risk.attributes["hold_in_progress"] is False
        assert "Celsius only" in risk.attributes["caveat"]
        assert hass.states.get(DAYS_UNTIL).state == "unknown"
        assert hass.states.get(PROGRESS).state == "unknown", (
            "0.0 minutes reads as a hold that has not started, not one that cannot"
        )


async def test_a_disinfection_policy_cannot_be_chosen_on_a_fahrenheit_system(hass, world):
    """Nothing could complete its cycle, so it would run to the give-up bound.

    The sensor that judges a hold is inert there, and a one-shot holds the
    element on in performance until a hold completes or the cycle gives up,
    days later.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    await setup_with_policy(hass)
    upstairs = entities(hass)["Upstairs"]
    before = upstairs.state

    for option in ("Once", "As needed (eco)"):
        await choose(hass, option)

        assert not upstairs._disinfecting
        assert upstairs.state == before
        assert upstairs._legionella_mode == LEGIONELLA_MODE_OFF
        assert hass.states.get(SELECT).state == "Off", (
            "the select kept showing a policy that is not running"
        )


def platforms_in_order(hass, first):
    """Force which platform sets up first: Home Assistant does not promise one.

    The heater and the policy select each restore the policy and hand it to the
    other, so every restore path has two orders, and each has gone wrong once.
    """
    forward = hass.config_entries.async_forward_entry_setups

    async def in_order(entry, platforms):
        for platform in sorted(platforms, key=lambda platform: platform != first):
            await forward(entry, [platform])

    return patch.object(
        hass.config_entries, "async_forward_entry_setups", side_effect=in_order
    )


RESTORED_CYCLES = {
    "policy and cycle": {
        "legionella_mode": LEGIONELLA_MODE_ASAP,
        "disinfection_active": True,
        "disinfection_return_mode": STATE_ELECTRIC,
    },
    "cycle with no policy beside it": {
        "disinfection_active": True,
        "disinfection_return_mode": STATE_ELECTRIC,
    },
}


@pytest.mark.parametrize("first", ["water_heater", "select"])
@pytest.mark.parametrize("restored", list(RESTORED_CYCLES))
async def test_a_cycle_restored_on_a_fahrenheit_system_is_ended(hass, world, restored, first):
    """One left running by a version that allowed it must not carry on.

    Restored as it was, it would hold performance with nothing able to end it
    but the give-up bound.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    with freeze_time(TRIGGER):
        mock_restore_cache(
            hass,
            (
                State(
                    UPSTAIRS,
                    STATE_PERFORMANCE,
                    {
                        "temperature": 60,
                        "disinfection_started_at": (TRIGGER - timedelta(hours=1)).isoformat(),
                        **RESTORED_CYCLES[restored],
                    },
                ),
                State(SELECT, "Once"),
            ),
        )
        with platforms_in_order(hass, first):
            await setup_with_policy(hass)

    upstairs = entities(hass)["Upstairs"]
    assert not upstairs._disinfecting
    assert upstairs._legionella_mode == LEGIONELLA_MODE_OFF
    assert upstairs.state == STATE_ELECTRIC, "the cycle's performance outlived it"
    assert hass.states.get(SELECT).state == "Off"


@pytest.mark.parametrize("first", ["water_heater", "select"])
async def test_a_policy_only_the_select_remembers_is_not_adopted_on_a_fahrenheit_system(
    hass, world, first
):
    """The select restores from its own history when the heater has none.

    Heater first: the select hands its policy over, and it must be refused.
    Select first: it is already showing that policy when the heater refuses
    it, and must be told.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    mock_restore_cache(hass, (State(SELECT, "As needed"),))
    with platforms_in_order(hass, first):
        await setup_with_policy(hass)

    upstairs = entities(hass)["Upstairs"]
    assert upstairs._legionella_mode == LEGIONELLA_MODE_OFF
    assert hass.states.get(SELECT).state == "Off"


async def test_changing_the_unit_system_takes_effect_without_a_restart(hass, world):
    """The unit is read when the entities load, and a unit change is not a restart.

    A sensor loaded in Celsius went on judging readings that had turned to
    Fahrenheit: the same hour at 61 F, recorded as a disinfection.
    """
    with freeze_time(START) as frozen:
        hass.states.async_set(UPSTAIRS_SENSOR, "45.0")
        await setup_entry(hass)
        assert "Celsius only" not in hass.states.get(RISK).attributes["caveat"]

        await hass.config.async_update(unit_system="us_customary")
        await hass.async_block_till_done()

        for minute in range(63):
            hass.states.async_set(
                UPSTAIRS_SENSOR, f"{61.0 + (minute % 2) * 0.1:.1f}", FAHRENHEIT
            )
            await hass.async_block_till_done()
            frozen.tick(timedelta(minutes=1))

        risk = hass.states.get(RISK)
        assert "Celsius only" in risk.attributes["caveat"]
        assert risk.state == "Unknown"
        assert risk.attributes["last_disinfection_at"] is None


async def test_other_core_settings_do_not_reload_the_heater(hass, world):
    """Only the temperature unit matters. A reload is not free: it rebuilds the
    heater and drops what lives only in memory, its timers among them."""
    await setup_both(hass)
    before = entities(hass)["Upstairs"]

    await hass.config.async_update(location_name="Somewhere else")
    await hass.async_block_till_done()

    assert entities(hass)["Upstairs"] is before


async def test_a_restored_policy_is_kept_on_a_metric_system(hass, world):
    """Guard: the same restore on a Celsius system is untouched."""
    with freeze_time(TRIGGER):
        mock_restore_cache(
            hass,
            (State(UPSTAIRS, STATE_ELECTRIC, {"temperature": 60, "legionella_mode": LEGIONELLA_MODE_ON}),),
        )
        await setup_with_policy(hass)

    assert entities(hass)["Upstairs"]._legionella_mode == LEGIONELLA_MODE_ON
