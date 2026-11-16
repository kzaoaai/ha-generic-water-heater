"""The one-shot Smart Eco bypass, and telling it apart from the timed pause.

Pressing ON is a bypass that ends by itself once the tank is satisfied. Turning
the tank off is a bypass that ends on a clock. They are different mechanisms and
they used to report the same label, which made the self-restoring one invisible
-- the owner reasonably concluded it did not exist and asked for it to be built.
"""

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_ON
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SENSOR,
    auto_enable_custom_integrations,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"


async def eco_permitting(hass):
    """Load both entries with Smart Eco enforcing and its condition true."""
    await setup_both(hass)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()


async def test_pressing_on_pauses_eco_until_the_tank_is_satisfied(hass, world):  # noqa: F811
    """No deadline: this pause resolves on the tank reaching target."""
    await eco_permitting(hass)

    await hass.services.async_call(
        "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()

    attrs = hass.states.get(UPSTAIRS).attributes
    assert attrs["smart_eco_pause_reason"] == "manual_on_wait_idle"
    assert attrs["smart_eco_resume_at"] is None, "the one-shot bypass grew a deadline"
    assert attrs["smart_eco_state"] == "Paused until the tank is satisfied", (
        "the label no longer says how this pause ends"
    )


async def test_pressing_on_a_tank_the_eco_gate_parked_also_waits_for_satisfaction(hass, world):  # noqa: F811
    """The usual case: eco has parked the tank at OFF, and a person presses ON.

    Leaving OFF is a heating boundary change, and set_operation_mode paused
    Smart Eco again for it, with the timed countdown, replacing turn_on's
    until-satisfied pause. The test above starts in electric and never crossed
    that boundary, so the bypass was unreachable from the state ON is most
    often pressed in.
    """
    await eco_permitting(hass)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).state == STATE_OFF, "test premise: eco parked it"

    await hass.services.async_call(
        "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get(UPSTAIRS)
    assert state.state == STATE_ELECTRIC
    assert state.attributes["smart_eco_pause_reason"] == "manual_on_wait_idle"
    assert state.attributes["smart_eco_resume_at"] is None
    assert state.attributes["smart_eco_state"] == "Paused until the tank is satisfied"


async def test_the_bypass_from_a_parked_tank_gives_eco_back_once_satisfied(hass, world):  # noqa: F811
    """And it ends the way it says: target reached, idle a minute, eco back."""
    await eco_permitting(hass)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
    await hass.services.async_call(
        "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).attributes["hvac_action"] == "heating"

    # Above the 60 C target, not at it: with both tolerances 0 a reading of
    # exactly 60 still asks for heat (the ON test comes first).
    # Two readings: a single jump of more than 10 C is held by the spike
    # filter until the next one confirms it.
    for reading in ("61", "61.1"):
        hass.states.async_set(UPSTAIRS_SENSOR, reading, {"device_class": "temperature"})
        await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).attributes["hvac_action"] == "idle"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    state = hass.states.get(UPSTAIRS)
    assert state.attributes["smart_eco_pause_reason"] is None
    assert state.state == STATE_OFF, "eco, blocking again, should have parked it"


async def test_turning_the_tank_off_takes_the_timed_path_instead(hass, world):  # noqa: F811
    """The other bypass ends on a clock, and must not borrow the same words."""
    await eco_permitting(hass)

    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()

    attrs = hass.states.get(UPSTAIRS).attributes
    assert attrs["smart_eco_pause_reason"] == "manual_off_timer"
    assert attrs["smart_eco_resume_at"] is not None
    assert attrs["smart_eco_state"] == "Paused by manual control", (
        f"timed pause reported {attrs['smart_eco_state']!r}"
    )


async def test_the_two_bypasses_never_report_the_same_label(hass, world):  # noqa: F811
    """The regression that made the self-restoring bypass undiscoverable."""
    await eco_permitting(hass)
    await hass.services.async_call(
        "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    on_label = hass.states.get(UPSTAIRS).attributes["smart_eco_state"]

    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    off_label = hass.states.get(UPSTAIRS).attributes["smart_eco_state"]

    assert on_label != off_label
