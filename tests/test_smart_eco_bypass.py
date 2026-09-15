"""The one-shot Smart Eco bypass, and telling it apart from the timed pause.

Pressing ON is a bypass that ends by itself once the tank is satisfied. Turning
the tank off is a bypass that ends on a clock. They are different mechanisms and
they used to report the same label, which made the self-restoring one invisible
-- the owner reasonably concluded it did not exist and asked for it to be built.
"""

from freezegun import freeze_time
from homeassistant.const import STATE_ON

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
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
    assert attrs["smart_eco_state"].startswith("Resuming in"), (
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
