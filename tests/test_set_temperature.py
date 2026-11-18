"""water_heater.set_temperature honours the operation_mode sent with it.

Core's service takes an optional operation_mode next to the required
temperature and passes it to the entity. The entity read only the temperature,
so a client that sent both got its target and silently lost its mode.
"""

import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.exceptions import ServiceValidationError

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)
from tests.test_load_shed import shed
from tests.test_wall_switch_performance import UPSTAIRS, hot_tank, wall_on

HEATER = UPSTAIRS_SWITCH


async def set_temperature(hass, temperature, **extra):
    """Call the core service the way a card or an automation does."""
    await hass.services.async_call(
        "water_heater",
        "set_temperature",
        {"entity_id": UPSTAIRS, "temperature": temperature, **extra},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_a_mode_sent_with_the_temperature_is_applied(hass, world):  # noqa: F811
    """Both halves of the call took effect; the mode used to be dropped."""
    upstairs = await hot_tank(hass)
    assert upstairs.state == STATE_ELECTRIC

    await set_temperature(hass, 55, operation_mode=STATE_PERFORMANCE)

    assert upstairs.state == STATE_PERFORMANCE
    assert upstairs.target_temperature == 55


async def test_an_off_sent_with_the_temperature_turns_the_element_off(hass, world):  # noqa: F811
    """The mode goes through the normal path, so the relay follows it."""
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await setup_both(hass, stagger_seconds=0.0)
    assert hass.states.get(HEATER).state == STATE_ON, "test premise: heating"

    await set_temperature(hass, 65, operation_mode=STATE_OFF)

    assert hass.states.get(UPSTAIRS).state == STATE_OFF
    assert hass.states.get(HEATER).state == STATE_OFF


@pytest.mark.parametrize("mode", ["eco", ""])
async def test_an_unknown_mode_is_refused_and_nothing_changes(hass, world, mode):  # noqa: F811
    """Core does not check the mode here, so the entity must, before acting."""
    upstairs = await hot_tank(hass)
    before = (upstairs.state, upstairs.target_temperature)
    world["turn_on"].clear()
    world["turn_off"].clear()

    with pytest.raises(ServiceValidationError) as refused:
        await set_temperature(hass, 70, operation_mode=mode)

    # Core's own message for set_operation_mode, so it renders: its key lives
    # in the water_heater domain's strings and takes these three placeholders.
    assert refused.value.translation_domain == "water_heater"
    assert refused.value.translation_key == "not_valid_operation_mode"
    assert refused.value.translation_placeholders == {
        "entity_id": UPSTAIRS,
        "operation_mode": mode,
        "operation_list": "electric, off, performance",
    }
    assert (upstairs.state, upstairs.target_temperature) == before
    assert upstairs.operation_list == [STATE_ELECTRIC, STATE_OFF, STATE_PERFORMANCE]
    assert not world["turn_on"] and not world["turn_off"]


async def test_a_temperature_alone_leaves_the_mode_and_the_pause_alone(hass, world):  # noqa: F811
    """Guard: passes on HEAD. A call without a mode must stay what it was."""
    upstairs = await hot_tank(hass)
    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    assert upstairs.state == STATE_OFF, "test premise"
    reason = upstairs._smart_eco_pause_reason
    assert reason is not None, "test premise: the person's OFF paused Smart Eco"

    await set_temperature(hass, 58)

    assert upstairs.state == STATE_OFF
    assert upstairs._smart_eco_pause_reason == reason
    assert upstairs.target_temperature == 58


async def test_repeating_the_current_mode_keeps_a_wall_switch_performance_run(hass, world):  # noqa: F811
    """Guard: passes on HEAD. Repeating the mode is not a new manual choice.

    A client that always sends both would otherwise clear the run's deadline
    and leave the promotion unbounded.
    """
    upstairs = await hot_tank(hass)
    await wall_on(hass)
    assert upstairs.state == STATE_PERFORMANCE
    until = upstairs._manual_performance_until
    assert until is not None, "test premise: a bounded run"

    await set_temperature(hass, 62, operation_mode=STATE_PERFORMANCE)

    assert upstairs.state == STATE_PERFORMANCE
    assert upstairs._manual_performance_until == until
    assert upstairs._manual_performance_timer is not None
    assert upstairs.target_temperature == 62


async def test_repeating_the_current_mode_does_not_lift_a_load_shed(hass, world):  # noqa: F811
    """Guard: passes on HEAD. A shed leaves the mode at electric.

    A card that sends that mode back with a new temperature is not a person
    overriding the load balancer. A different mode is, as it always was.
    """
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await setup_both(hass, stagger_seconds=0.0)
    upstairs = entities(hass)["Upstairs"]
    await shed(hass, UPSTAIRS)
    assert upstairs.state == STATE_ELECTRIC and upstairs._load_shed, "test premise"

    await set_temperature(hass, 65, operation_mode=STATE_ELECTRIC)

    assert upstairs._load_shed
    assert hass.states.get(HEATER).state == STATE_OFF

    await set_temperature(hass, 65, operation_mode=STATE_PERFORMANCE)

    assert not upstairs._load_shed
    assert hass.states.get(HEATER).state == STATE_ON


async def test_choosing_electric_for_an_eco_parked_tank_is_the_persons_choice(hass, world):  # noqa: F811
    """A tank Smart Eco has parked reports OFF, so electric is a different mode.

    It is taken exactly as set_operation_mode takes it: Smart Eco is paused for
    the person and the tank heats toward the target sent in the same call.
    """
    upstairs = await hot_tank(hass, pv=STATE_OFF)
    assert upstairs.state == STATE_OFF, "test premise: eco parked it"
    assert upstairs._smart_eco_pause_reason is None, "test premise"

    await set_temperature(hass, 75, operation_mode=STATE_ELECTRIC)

    assert upstairs.state == STATE_ELECTRIC
    assert upstairs._smart_eco_pause_reason is not None, (
        "the mode was written without going through set_operation_mode"
    )
    assert hass.states.get(HEATER).state == STATE_ON, "the tank is below the new target"


async def test_repeating_the_off_of_an_eco_parked_tank_changes_only_the_target(hass, world):  # noqa: F811
    """Guard: passes on HEAD. OFF is what a client reads back from a parked tank.

    Sending it again with a new temperature is not a person turning the tank
    off: Smart Eco stays in charge and heats when its condition returns.
    """
    upstairs = await hot_tank(hass, pv=STATE_OFF)
    assert upstairs.state == STATE_OFF, "test premise: eco parked it"

    await set_temperature(hass, 75, operation_mode=STATE_OFF)

    assert upstairs.state == STATE_OFF
    assert upstairs._smart_eco_pause_reason is None
    assert upstairs.target_temperature == 75

    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    assert upstairs.state == STATE_ELECTRIC
    assert hass.states.get(HEATER).state == STATE_ON
