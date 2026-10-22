"""The eco condition's named inputs: a surplus entity, a utility entity, a template.

Heating is allowed while surplus is active, OR the utility is active and the
"Eco: Use Utility" switch is on, OR the (optional) template is true. An entity
is active when its state equals its configured active state, ignoring case, so
an enum power-source sensor works as well as a binary sensor.
"""

import pytest
from unittest.mock import patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.generic_water_heater import (
    CONF_ECO_TEMPLATE,
    CONF_SURPLUS_BOOST_TARGET,
    CONF_SURPLUS_ENTITY,
    CONF_SURPLUS_STATE,
    CONF_UTILITY_ENTITY,
    CONF_UTILITY_STATE,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

UPSTAIRS = "water_heater.upstairs"
SOURCE = "sensor.home_power_source"
USE_UTILITY = "switch.upstairs_eco_use_utility"
BOOST = "switch.upstairs_boost_use_pv_excess"


async def setup_inputs(
    hass,
    *,
    temperature="40",
    target=50.0,
    surplus=True,
    utility=True,
    template="",
    boost=None,
    source="Battery",
    excess=STATE_OFF,
    **extra,
):
    """One heater on named inputs. The template is off unless given."""
    hass.states.async_set(UPSTAIRS_SENSOR, temperature, {"device_class": "temperature"})
    hass.states.async_set(PV_EXCESS, excess)
    hass.states.async_set(SOURCE, source)
    data = {CONF_ECO_TEMPLATE: template}
    if surplus:
        data[CONF_SURPLUS_ENTITY] = PV_EXCESS
        data[CONF_SURPLUS_STATE] = "on"
    if utility:
        data[CONF_UTILITY_ENTITY] = SOURCE
        data[CONF_UTILITY_STATE] = "Utility"
    if boost is not None:
        data[CONF_SURPLUS_BOOST_TARGET] = boost
    entry = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        stagger_seconds=0.0, target_temp=target, **data, **extra,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def heater(hass):
    return hass.states.get(UPSTAIRS)


def switch_on(hass):
    return hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


async def set_state(hass, entity_id, state):
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()


async def test_nothing_active_parks_the_tank(hass, world):  # noqa: F811
    await setup_inputs(hass)
    assert heater(hass).state == STATE_OFF
    assert heater(hass).attributes["smart_eco_condition_met"] is False
    assert not switch_on(hass)


async def test_surplus_alone_allows_heating(hass, world):  # noqa: F811
    await setup_inputs(hass, utility=False)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert heater(hass).attributes["smart_eco_condition_met"] is True
    assert switch_on(hass)


async def test_an_enum_utility_state_allows_heating(hass, world):  # noqa: F811
    """The power-source sensor reads Solar / Battery / Utility, not on/off."""
    await setup_inputs(hass, surplus=False)
    assert not switch_on(hass)
    await set_state(hass, SOURCE, "Utility")
    assert switch_on(hass)
    await set_state(hass, SOURCE, "Battery")
    assert not switch_on(hass)


async def test_the_active_state_ignores_case(hass, world):  # noqa: F811
    await setup_inputs(hass, surplus=False)
    await set_state(hass, SOURCE, "utility")
    assert switch_on(hass)


async def test_an_unavailable_input_does_not_allow_heating(hass, world):  # noqa: F811
    await setup_inputs(hass, surplus=False, source="Utility")
    assert switch_on(hass)
    await set_state(hass, SOURCE, STATE_UNAVAILABLE)
    assert not switch_on(hass)


async def test_use_utility_off_ignores_the_utility(hass, world):  # noqa: F811
    await setup_inputs(hass, source="Utility")
    assert switch_on(hass)

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": USE_UTILITY}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(USE_UTILITY).state == STATE_OFF
    assert heater(hass).attributes["eco_use_utility"] is False
    assert not switch_on(hass)

    # Surplus still counts: the switch takes out only the utility term.
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)


async def test_the_switch_is_a_policy_choice_not_a_manual_override(hass, world):  # noqa: F811
    """Toggling it must not start a Smart Eco pause the way a person's flip does."""
    await setup_inputs(hass, source="Utility")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": USE_UTILITY}, blocking=True
    )
    await hass.async_block_till_done()
    assert heater(hass).attributes["smart_eco_pause_reason"] is None


async def test_the_template_is_an_extra_term(hass, world):  # noqa: F811
    await setup_inputs(hass, template="{{ true }}")
    assert heater(hass).attributes["smart_eco_condition_met"] is True
    assert switch_on(hass)


async def test_entities_alone_give_smart_eco_its_entities(hass, world):  # noqa: F811
    """No template at all: the select and state sensor still exist."""
    await setup_inputs(hass)
    assert hass.states.get("select.upstairs_smart_eco_mode") is not None
    assert hass.states.get("sensor.upstairs_smart_eco_state") is not None


async def test_switches_exist_only_for_configured_inputs(hass, world):  # noqa: F811
    await setup_inputs(hass, utility=False)
    assert hass.states.get(USE_UTILITY) is None
    # A surplus entity without a boost target has nothing to boost to.
    assert hass.states.get(BOOST) is None


async def test_both_switches_exist_when_configured(hass, world):  # noqa: F811
    await setup_inputs(hass, boost=61.0)
    assert hass.states.get(USE_UTILITY).state == STATE_ON
    assert hass.states.get(BOOST).state == STATE_OFF


@pytest.mark.parametrize("first", ["water_heater", "switch"])
async def test_switch_states_restore_from_the_heater(hass, world, first):  # noqa: F811
    """The heater owns the switches' state; either platform order shows it."""
    forward = hass.config_entries.async_forward_entry_setups

    async def in_order(entry, platforms):
        for platform in sorted(platforms, key=lambda platform: platform != first):
            await forward(entry, [platform])

    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                "electric",
                {"eco_use_utility": False, "surplus_boost_enabled": True},
            ),
        ),
    )
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", side_effect=in_order
    ):
        await setup_inputs(hass, boost=61.0, source="Utility")

    assert hass.states.get(USE_UTILITY).state == STATE_OFF
    assert hass.states.get(BOOST).state == STATE_ON
    # And the heater acts on it: the utility is on, but not used.
    assert not switch_on(hass)
