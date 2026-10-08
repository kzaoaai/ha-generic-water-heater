"""The `eco_gated` attribute: does this heat depend on the eco condition?

A consumer (a battery runtime estimate) leaves an eco-gated heater's metered
draw out of what the battery must carry when the gating supply leaves - the eco
condition goes false and the heater stops. So a False here is always safe and a
false True hides real load: every way of heating REGARDLESS of the condition
must read False.
"""

from homeassistant.components.water_heater import STATE_PERFORMANCE
from homeassistant.const import STATE_ON

from custom_components.generic_water_heater import (
    DOMAIN,
    SERVICE_SHED,
    SMART_ECO_MODE_OFF,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"


async def _eco_heating(hass):
    await setup_both(hass, stagger_seconds=0.0)
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    return entities(hass)["Upstairs"]


def _gated(hass):
    return hass.states.get(UPSTAIRS).attributes["eco_gated"]


async def test_eco_driven_heating_is_gated(hass, world):
    await _eco_heating(hass)
    assert hass.states.get(UPSTAIRS).attributes["hvac_action"] == "heating"
    assert _gated(hass) is True


async def test_performance_heats_regardless(hass, world):
    heater = await _eco_heating(hass)
    await heater.async_set_operation_mode(STATE_PERFORMANCE)
    await hass.async_block_till_done()
    assert _gated(hass) is False


async def test_smart_eco_off_heats_regardless(hass, world):
    heater = await _eco_heating(hass)
    await heater.async_set_smart_eco_mode(SMART_ECO_MODE_OFF, source="smart_eco_select")
    await hass.async_block_till_done()
    assert _gated(hass) is False


async def test_a_paused_policy_heats_regardless(hass, world):
    """A manual override pauses Smart Eco: the condition no longer decides."""
    heater = await _eco_heating(hass)
    heater._smart_eco_pause_reason = "until_manual"
    heater.async_write_ha_state()
    assert _gated(hass) is False


async def test_a_disinfection_cycle_heats_regardless(hass, world):
    heater = await _eco_heating(hass)
    heater._disinfecting = True
    heater.async_write_ha_state()
    assert _gated(hass) is False


async def test_a_shed_heater_is_not_gated(hass, world):
    await _eco_heating(hass)
    await hass.services.async_call(
        DOMAIN, SERVICE_SHED, {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    assert _gated(hass) is False


async def test_the_startup_grace_heats_regardless(hass, world):
    """Inside the grace a FALSE condition neither parks nor restores: the restored
    mode stands and the heater may heat with the condition false (review
    2026-10-08, sixth pass). Not gated until the grace ends."""
    heater = await _eco_heating(hass)
    heater._startup_grace_active = True
    heater.async_write_ha_state()
    assert _gated(hass) is False


async def test_a_false_condition_is_never_gated(hass, world):
    """Gated means the condition is what allows the heat right now."""
    heater = await _eco_heating(hass)
    heater._eco_condition_met = False
    heater.async_write_ha_state()
    assert _gated(hass) is False
