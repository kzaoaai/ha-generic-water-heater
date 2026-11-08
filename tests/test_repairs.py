"""Outages that last are raised in Repairs; the failsafe warns once per outage."""

import logging
from datetime import timedelta

from freezegun import freeze_time
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import issue_registry as ir
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater import DOMAIN
from tests.test_disinfection import choose, gave_up, report_risk, setup_with_policy
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    TRIGGER,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    setup_both,
    world,
)


def issue(hass, kind, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


async def tick(hass, frozen, minutes):
    frozen.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_a_lasting_sensor_outage_is_raised_and_clears_itself(hass, world):
    with freeze_time(TRIGGER) as frozen:
        upstairs, _ = await setup_both(hass)
        hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await tick(hass, frozen, 9)
        assert issue(hass, "temperature_sensor_unavailable", upstairs) is None
        await tick(hass, frozen, 2)
        raised = issue(hass, "temperature_sensor_unavailable", upstairs)
        assert raised is not None
        assert raised.translation_placeholders["entity_id"] == UPSTAIRS_SENSOR

        hass.states.async_set(UPSTAIRS_SENSOR, "41.0")
        await hass.async_block_till_done()
        assert issue(hass, "temperature_sensor_unavailable", upstairs) is None


async def test_a_short_dropout_raises_nothing(hass, world):
    with freeze_time(TRIGGER) as frozen:
        upstairs, _ = await setup_both(hass)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await tick(hass, frozen, 3)
        hass.states.async_set(UPSTAIRS_SWITCH, "off")
        await hass.async_block_till_done()
        await tick(hass, frozen, 20)
        assert issue(hass, "heater_switch_unavailable", upstairs) is None


async def test_a_lasting_switch_outage_is_raised_and_clears_itself(hass, world):
    with freeze_time(TRIGGER) as frozen:
        upstairs, _ = await setup_both(hass)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await tick(hass, frozen, 11)
        assert issue(hass, "heater_switch_unavailable", upstairs) is not None
        hass.states.async_set(UPSTAIRS_SWITCH, "off")
        await hass.async_block_till_done()
        assert issue(hass, "heater_switch_unavailable", upstairs) is None


async def test_the_failsafe_warns_once_per_outage(hass, world, caplog):
    await setup_both(hass)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
            await hass.async_block_till_done()
            hass.states.async_set(UPSTAIRS_SENSOR, "unknown")
            await hass.async_block_till_done()
    warnings = [r for r in caplog.records if "entering Failsafe" in r.getMessage()]
    assert len(warnings) == 1, f"{len(warnings)} failsafe warnings for one outage"


async def test_a_give_up_repair_clears_when_the_policy_changes(hass, world):
    """A standing policy that gave up stays armed; turning it off clears the repair."""
    with freeze_time(TRIGGER):
        upstairs, _ = await setup_with_policy(hass)
        await report_risk(hass, upstairs, "Elevated")
        await choose(hass, "As needed (eco)")
    with freeze_time(TRIGGER + timedelta(days=4)):
        hass.states.async_set(UPSTAIRS_SENSOR, "44.0")
        await hass.async_block_till_done()
        assert gave_up(hass, upstairs) is not None
        await choose(hass, "Off")
        assert gave_up(hass, upstairs) is None


async def test_a_give_up_repair_clears_when_a_new_cycle_starts(hass, world):
    with freeze_time(TRIGGER):
        upstairs, _ = await setup_with_policy(hass)
        await report_risk(hass, upstairs, "Elevated")
        await choose(hass, "Once (eco)")
    with freeze_time(TRIGGER + timedelta(days=4)):
        hass.states.async_set(UPSTAIRS_SENSOR, "44.0")
        await hass.async_block_till_done()
        assert gave_up(hass, upstairs) is not None
        await choose(hass, "Once (eco)")
        assert gave_up(hass, upstairs) is None


async def test_removing_the_entry_removes_its_repairs(hass, world):
    with freeze_time(TRIGGER) as frozen:
        upstairs, _ = await setup_both(hass)
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await tick(hass, frozen, 11)
        assert issue(hass, "heater_switch_unavailable", upstairs) is not None
        await hass.config_entries.async_remove(upstairs.entry_id)
        await hass.async_block_till_done()
    assert issue(hass, "heater_switch_unavailable", upstairs) is None
