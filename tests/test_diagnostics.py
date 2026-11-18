"""Diagnostics: one JSON download with the in-memory state."""

import json

from custom_components.generic_water_heater.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)


async def test_diagnostics_are_json_and_carry_the_internal_state(hass, world):
    upstairs, _ = await setup_both(hass)
    diag = await async_get_config_entry_diagnostics(hass, upstairs)

    json.dumps(diag)  # must serialise as-is
    assert diag["entry"]["data"]["name"] == "Upstairs"
    heater = diag["heater"]
    assert heater["entity_id"] == "water_heater.upstairs"
    assert heater["current_temperature"] == 40.0
    assert set(heater["timers_armed"]) >= {"_cooldown_timer", "_switch_outage_timer"}
    assert "eco_gated" in heater["attributes"]
    assert diag["fleet"]["member"]["entry_id"] == upstairs.entry_id
    assert not any(key.endswith("_entity") for key in diag["runtime"])


async def test_diagnostics_report_every_timer_the_heater_holds(hass, world):
    """The list was kept by hand and had fallen two timers behind the entity.

    The failsafe warning and the switch-unavailable timers were added to the
    heater and never to the download, so the two outages a download is most
    often taken for were the two it could not show as pending.
    """
    upstairs, _ = await setup_both(hass)
    heater = entities(hass)["Upstairs"]
    held = {name for name in vars(heater) if name.endswith("_timer")}
    assert held, "test premise: the heater keeps its timers in *_timer attributes"

    diag = await async_get_config_entry_diagnostics(hass, upstairs)

    assert set(diag["heater"]["timers_armed"]) == held
