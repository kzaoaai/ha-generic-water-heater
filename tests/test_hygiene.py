"""4.6.1 housekeeping: one switch per entry, reload only on change, the select restore."""

import json
import pathlib
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import State
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache
import yaml

from custom_components.generic_water_heater import (
    DOMAIN,
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_UNTIL_MANUAL,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    DOWNSTAIRS_SWITCH,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    entities,
    setup_both,
    world,
)

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components/generic_water_heater"
UPSTAIRS = "water_heater.upstairs"
SMART_ECO_SELECT = "select.upstairs_smart_eco_mode"


def form(name, switch):
    return {
        "name": name,
        "heater_switch": switch,
        "temperature_sensor": UPSTAIRS_SENSOR,
        "temperatures": {},
        "cycle_protection": {},
        "smart_eco": {},
        "surplus_boost": {},
        "fleet": {},
        "legionella": {},
        "hot_water_in_use": {},
        "metering": {},
        "advanced": {},
    }


async def test_a_second_entry_on_the_same_switch_is_refused(hass, world):
    await setup_both(hass)
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], form("Again", UPSTAIRS_SWITCH)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "heater_already_configured"}


async def test_options_cannot_move_an_entry_onto_a_siblings_switch(hass, world):
    upstairs, _ = await setup_both(hass)
    flow = await hass.config_entries.options.async_init(upstairs.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], form("Upstairs", DOWNSTAIRS_SWITCH)
    )
    assert result["errors"] == {"base": "heater_already_configured"}


async def test_an_entry_keeping_its_own_switch_is_not_refused(hass, world):
    upstairs, _ = await setup_both(hass)
    flow = await hass.config_entries.options.async_init(upstairs.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], form("Upstairs", UPSTAIRS_SWITCH)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_renaming_the_entry_does_not_reload_it(hass, world):
    """A reload drops in-memory state; a title change has nothing to apply."""
    upstairs, _ = await setup_both(hass)
    before = entities(hass)["Upstairs"]

    hass.config_entries.async_update_entry(upstairs, title="Renamed")
    await hass.async_block_till_done()

    assert entities(hass)["Upstairs"] is before


async def test_an_options_change_still_reloads(hass, world):
    upstairs, _ = await setup_both(hass)
    before = entities(hass)["Upstairs"]

    hass.config_entries.async_update_entry(
        upstairs, options={**upstairs.data, "fleet_stagger_seconds": 90.0}
    )
    await hass.async_block_till_done()

    assert entities(hass)["Upstairs"] is not before


async def test_an_entry_from_a_newer_release_is_refused(hass, world):
    """Enforced by Home Assistant itself (entry version above the flow's VERSION),
    not by async_migrate_entry: this pins the behaviour, not code of ours."""
    current = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR)
    entry = MockConfigEntry(domain=DOMAIN, title="Upstairs", version=5, data=dict(current.data))
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


def test_every_registered_service_is_described_and_translated():
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    translations = json.loads((COMPONENT / "translations/en.json").read_text())
    assert set(services) == {"shed", "release"}
    assert set(translations["services"]) == set(services)


def test_the_manifest_points_at_this_repository():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert "ha-generic-water-heater" in manifest["issue_tracker"]
    assert "ha-generic-water-heater" in manifest["documentation"]
    assert "dependencies" not in manifest


# ---------------------------------------------------------------------------
# Smart Eco mode: the heater's history wins, in either platform order
# ---------------------------------------------------------------------------


def order(hass, first):
    forward = hass.config_entries.async_forward_entry_setups

    async def in_order(entry, platforms):
        for platform in sorted(platforms, key=lambda platform: platform != first):
            await forward(entry, [platform])

    return patch.object(
        hass.config_entries, "async_forward_entry_setups", side_effect=in_order
    )


@pytest.mark.parametrize("first", ["water_heater", "select"])
async def test_the_heaters_smart_eco_history_wins(hass, world, first):
    mock_restore_cache(
        hass,
        (
            State(UPSTAIRS, "electric", {"temperature": 60, "smart_eco_mode": SMART_ECO_MODE_UNTIL_MANUAL}),
            State(SMART_ECO_SELECT, "Always ON"),
        ),
    )
    with order(hass, first):
        await setup_both(hass)

    assert entities(hass)["Upstairs"]._smart_eco_mode == SMART_ECO_MODE_UNTIL_MANUAL
    assert hass.states.get(SMART_ECO_SELECT).state == "On until next manual control"


@pytest.mark.parametrize("first", ["water_heater", "select"])
async def test_the_selects_history_is_used_when_the_heater_has_none(hass, world, first):
    mock_restore_cache(hass, (State(SMART_ECO_SELECT, "Always ON"),))
    with order(hass, first):
        await setup_both(hass)

    assert entities(hass)["Upstairs"]._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON
    assert hass.states.get(SMART_ECO_SELECT).state == "Always ON"


async def test_temperatures_are_served_under_the_native_names_too(hass, world):
    """2026.11 reads native_*; the plain names stop working in 2027.11.

    Coverage note: the suite runs on 2026.10, which reads only the plain
    names, so this pins that both are provided and agree. That 2026.11 then
    logs no deprecation was checked by importing the module on 2026.11.0.
    """
    await setup_both(hass)
    upstairs = entities(hass)["Upstairs"]
    assert upstairs.native_current_temperature == upstairs.current_temperature == 40.0
    assert upstairs.native_target_temperature == upstairs.target_temperature
    assert upstairs.native_temperature_unit == upstairs.temperature_unit
