"""Load shedding must be distinguishable from a person operating the heater.

An external load balancer sheds this heater to protect the supply. Before the
shed/release services existed it had to call set_operation_mode, which is
indistinguishable from someone using the UI -- so every shed paused Smart Eco
for hours, and every restore paused it again, leaving the tank heating off-PV.
"""

from datetime import timedelta

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.generic_water_heater import (
    DOMAIN,
    SERVICE_RELEASE,
    SERVICE_SHED,
    SMART_ECO_MODE_ALWAYS_ON,
    async_get_fleet,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    commanded,
    entities,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"
DOWNSTAIRS = "water_heater.downstairs"


async def shed(hass, entity_id):
    """Ask the load balancer's shed service to drop a heater."""
    await hass.services.async_call(
        DOMAIN, SERVICE_SHED, {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def release(hass, entity_id):
    """Release a previously shed heater."""
    await hass.services.async_call(
        DOMAIN, SERVICE_RELEASE, {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()


# The test entries configure min_off_duration as 0, which the integration reads
# as falsy and replaces with its 120 s default, so a heater that has just been
# switched off cannot come back for that long. Ticking past it keeps these tests
# about shedding rather than about the anti-short-cycle hold.
MIN_OFF_HOLD = timedelta(seconds=130)


async def heating(hass, world):
    """Bring both heaters into eco-driven heating and return the entities."""
    await setup_both(hass, stagger_seconds=0.0)
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    world["turn_on"].clear()
    world["turn_off"].clear()
    return entities(hass)


async def test_services_are_registered(hass, world):
    """The load balancer needs both services to exist to call them."""
    await setup_both(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_SHED)
    assert hass.services.has_service(DOMAIN, SERVICE_RELEASE)


async def test_shed_drops_the_heater_without_touching_smart_eco(hass, world):
    """The whole point: shedding must not look like a person."""
    live = await heating(hass, world)
    upstairs = live["Upstairs"]
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON

    await shed(hass, UPSTAIRS)

    assert commanded(world["turn_off"]) == [UPSTAIRS_SWITCH]
    assert upstairs._load_shed is True
    # Smart Eco policy is exactly as it was -- no pause, no 3-6 hour countdown.
    assert upstairs._smart_eco_pause_reason is None
    assert upstairs._smart_eco_resume_at is None
    assert upstairs._is_smart_eco_enforcing()
    # The mode the user had is preserved, not overwritten with "off".
    assert upstairs._current_operation != STATE_OFF
    assert hass.states.get(UPSTAIRS).attributes["load_shed"] is True


async def test_smart_eco_cannot_undo_a_shed(hass, world):
    """Eco is still enforcing and the PV condition is still true -- stay off."""
    live = await heating(hass, world)
    upstairs = live["Upstairs"]

    await shed(hass, UPSTAIRS)
    world["turn_on"].clear()

    # Several control passes, exactly what a temperature update would drive.
    for _ in range(3):
        await upstairs._async_control_heating()
        await hass.async_block_till_done()

    assert commanded(world["turn_on"]) == [], "Smart Eco restored a shed heater"
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_release_resumes_without_a_hangover(hass, world):
    """Releasing gives back exactly the policy the heater already had."""
    with freeze_time(TRIGGER) as frozen:
        live = await heating(hass, world)
        upstairs = live["Upstairs"]

        await shed(hass, UPSTAIRS)
        world["turn_on"].clear()
        frozen.tick(MIN_OFF_HOLD)
        await release(hass, UPSTAIRS)

    assert commanded(world["turn_on"]) == [UPSTAIRS_SWITCH]
    assert upstairs._load_shed is False
    assert upstairs._smart_eco_pause_reason is None, "release left an eco pause behind"
    assert upstairs._is_smart_eco_enforcing()


async def test_a_deliberate_boost_survives_a_shed_and_release(hass, world):
    """A purposeful ON is still there afterwards, eco pause and all."""
    with freeze_time(TRIGGER) as frozen:
        live = await heating(hass, world)
        upstairs = live["Upstairs"]

        # Someone switches it off, then deliberately boosts it back. Both cross
        # a heating boundary, so Smart Eco steps aside for them by design.
        await upstairs.async_set_operation_mode(STATE_OFF)
        # Let the switch actually report off before issuing the next command,
        # the way a real gap between two deliberate actions would.
        await hass.async_block_till_done()
        frozen.tick(MIN_OFF_HOLD)
        await upstairs.async_set_operation_mode(STATE_PERFORMANCE)
        await hass.async_block_till_done()
        paused_reason = upstairs._smart_eco_pause_reason
        resume_at = upstairs._smart_eco_resume_at
        assert paused_reason is not None, "expected a manual override pause"
        assert not upstairs._is_smart_eco_enforcing()

        await shed(hass, UPSTAIRS)
        world["turn_on"].clear()
        frozen.tick(MIN_OFF_HOLD)
        await release(hass, UPSTAIRS)

    # Same mode, same pause, same countdown -- the shed changed nothing.
    assert upstairs._current_operation == STATE_PERFORMANCE
    assert upstairs._smart_eco_pause_reason == paused_reason
    assert upstairs._smart_eco_resume_at == resume_at
    assert commanded(world["turn_on"]) == [UPSTAIRS_SWITCH]


async def test_a_person_asking_for_heat_beats_a_shed(hass, world):
    """Human wins: the balancer may re-shed, but the person is obeyed first."""
    with freeze_time(TRIGGER) as frozen:
        live = await heating(hass, world)
        upstairs = live["Upstairs"]

        await shed(hass, UPSTAIRS)
        world["turn_on"].clear()
        frozen.tick(MIN_OFF_HOLD)

        await upstairs.async_turn_on()
        await hass.async_block_till_done()

    assert upstairs._load_shed is False
    assert commanded(world["turn_on"]) == [UPSTAIRS_SWITCH]


async def test_the_physical_switch_beats_a_shed(hass, world):
    """Someone at the wall wins too, same as everywhere else in this component."""
    live = await heating(hass, world)
    upstairs = live["Upstairs"]

    await shed(hass, UPSTAIRS)
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF

    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    await hass.async_block_till_done()

    assert upstairs._load_shed is False
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON


async def test_a_request_to_turn_off_does_not_clear_a_shed(hass, world):
    """Only a request for heat clears a shed; an OFF has nothing to win."""
    live = await heating(hass, world)
    upstairs = live["Upstairs"]

    await shed(hass, UPSTAIRS)
    await upstairs.async_turn_off()
    await hass.async_block_till_done()

    assert upstairs._load_shed is True


async def test_shed_outranks_smart_eco_always_on(hass, world):
    """Always ON means eco will not stop you, not that the supply cannot."""
    live = await heating(hass, world)
    upstairs = live["Upstairs"]
    await upstairs.async_set_smart_eco_mode(SMART_ECO_MODE_ALWAYS_ON, source="test")
    await upstairs._async_control_heating()
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON
    world["turn_off"].clear()

    await shed(hass, UPSTAIRS)

    assert commanded(world["turn_off"]) == [UPSTAIRS_SWITCH], (
        "Always ON swallowed the shed -- the balancer freed no capacity"
    )
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF


async def test_shed_hands_its_watts_back_to_the_fleet(hass, world):
    """A shed frees budget for the sibling, same as any other turn-off."""
    with freeze_time(TRIGGER) as frozen:
        await setup_both(hass, budget_w=2500.0, stagger_seconds=0.0)
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        fleet = async_get_fleet(hass)

        for _ in range(2):
            frozen.tick(timedelta(seconds=2))
            for entity in entities(hass).values():
                await entity._async_control_heating()
            await hass.async_block_till_done()

        holder = next(e for e in entities(hass).values() if fleet.get(e._entry_id).committed)
        sibling = next(e for e in entities(hass).values() if e is not holder)
        assert fleet.committed_power_w == holder._nominal_power_w

        await shed(hass, f"water_heater.{holder.name.lower()}")

    # The shed heater gives its share back, and the capacity is handed straight
    # to the sibling that was waiting on it.
    assert fleet.get(holder._entry_id).committed is False
    assert fleet.get(sibling._entry_id).committed is True
    assert fleet.committed_power_w == sibling._nominal_power_w
