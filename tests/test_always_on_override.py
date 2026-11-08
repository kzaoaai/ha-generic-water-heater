"""Always ON ignores an OFF -- except the wall switch's, which starts a timed override.

Before 4.6.2 the guard that ignores an OFF ran after two things it should have
preceded: the wall switch's override had already been started and announced,
and a running disinfection cycle had already been ended (policy set to Off).
So the wall OFF was announced and then not honoured -- the element came back
on after min_off -- and an ignored OFF still cancelled a cycle.
"""

from homeassistant.components.water_heater import STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.generic_water_heater import SMART_ECO_MODE_ALWAYS_ON
from tests.test_disinfection import (  # noqa: F401
    SELECT,
    UPSTAIRS,
    choose,
    report_risk,
    setup_with_policy,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)


async def always_on_and_heating(hass):
    await setup_both(hass, stagger_seconds=0.0)
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    upstairs = entities(hass)["Upstairs"]
    await upstairs.async_set_smart_eco_mode(SMART_ECO_MODE_ALWAYS_ON, source="test")
    await upstairs._async_control_heating()
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON, "test premise"
    return upstairs


async def test_a_wall_off_under_always_on_keeps_the_tank_off(hass, world):
    upstairs = await always_on_and_heating(hass)

    hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)  # a person at the wall
    await hass.async_block_till_done()

    assert upstairs._smart_eco_pause_reason == "manual_off_timer"
    assert hass.states.get(UPSTAIRS).state == STATE_OFF
    # min_off is 0 here, so a control pass that ignored the OFF would already
    # have switched the element back on.
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_an_entity_off_under_always_on_is_still_ignored(hass, world):
    """The policy itself is unchanged: only the wall switch overrides it.

    Coverage note: this passes with the ignore guard removed, because Always
    ON's own restore branch lifts an OFF straight back to heating. What pins
    the guard is the disinfection test below, where ignoring must happen
    before the cycle is ended.
    """
    upstairs = await always_on_and_heating(hass)

    await hass.services.async_call(
        "water_heater", "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_OFF}, blocking=True,
    )
    await hass.async_block_till_done()

    assert upstairs._smart_eco_pause_reason is None
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


async def test_an_ignored_off_does_not_cancel_a_disinfection_cycle(hass, world):
    upstairs_entry, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs_entry, "Elevated")
    await choose(hass, "Once (eco)")
    upstairs = entities(hass)["Upstairs"]
    await upstairs.async_set_smart_eco_mode(SMART_ECO_MODE_ALWAYS_ON, source="test")
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True

    # e.g. Apple Home's off, which arrives as set_operation_mode.
    await hass.services.async_call(
        "water_heater", "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_OFF}, blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(UPSTAIRS)
    assert state.attributes["disinfection_active"] is True
    assert state.state == STATE_PERFORMANCE
    assert hass.states.get(SELECT).state == "Once (eco)"
