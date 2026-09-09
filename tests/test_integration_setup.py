"""End-to-end tests: two config entries sharing one Smart Eco template.

This is the 2026-08-18 incident in full. Both entries carry the same eco
template, so when the shared trigger flips, Home Assistant fires both template
listeners in a single event-loop pass and both instances run their control
logic before either switch has reported back.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_water_heater import (
    CONF_COLD_TOLERANCE,
    CONF_ECO_TEMPLATE,
    CONF_FLEET_POWER_BUDGET_W,
    CONF_FLEET_STAGGER_SECONDS,
    CONF_HEATER,
    CONF_HOT_TOLERANCE,
    CONF_NOMINAL_POWER_W,
    CONF_SENSOR,
    CONF_TARGET_TEMP,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DOMAIN,
    async_get_fleet,
)
from custom_components.generic_water_heater.fleet import (
    UNAVAILABLE_COMMITMENT_SECONDS,
    FLEET_KEY,
)

PV_EXCESS = "binary_sensor.pv_power_excess"
ECO_TEMPLATE = "{{ is_state('binary_sensor.pv_power_excess', 'on') }}"

UPSTAIRS_SWITCH = "switch.upstairs_element"
DOWNSTAIRS_SWITCH = "switch.downstairs_element"
UPSTAIRS_SENSOR = "sensor.upstairs_water_temperature"
DOWNSTAIRS_SENSOR = "sensor.downstairs_water_temperature"

TRIGGER = datetime(2026, 8, 18, 13, 51, 26, 785000, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load this custom integration."""
    yield


def build_entry(
    name,
    switch,
    sensor,
    nominal_power_w,
    budget_w=0.0,
    stagger_seconds=60.0,
    target_temp=60.0,
    cold_tolerance=0.0,
    hot_tolerance=0.0,
):
    """Return a config entry shaped like the two real ones."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        version=4,
        data={
            CONF_NAME: name,
            CONF_HEATER: switch,
            CONF_SENSOR: sensor,
            CONF_TARGET_TEMP: target_temp,
            CONF_TEMP_STEP: 1.0,
            CONF_COLD_TOLERANCE: cold_tolerance,
            CONF_HOT_TOLERANCE: hot_tolerance,
            CONF_TEMP_MIN: 15.0,
            CONF_TEMP_MAX: 80.0,
            "min_on_duration": {"seconds": 0},
            "min_off_duration": {"seconds": 0},
            CONF_ECO_TEMPLATE: ECO_TEMPLATE,
            CONF_NOMINAL_POWER_W: nominal_power_w,
            CONF_FLEET_STAGGER_SECONDS: stagger_seconds,
            CONF_FLEET_POWER_BUDGET_W: budget_w,
        },
    )


@pytest.fixture
def world(hass):
    """Set up switches, sensors and switch-reflecting services, PV excess off.

    The turn_on/turn_off handlers actually move the switch state, the way a real
    switch does. Without that the entities never see a state-change event and
    _async_switch_changed -- which is where manual override detection lives --
    would never run at all.
    """
    hass.states.async_set(PV_EXCESS, STATE_OFF)
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
    hass.states.async_set(DOWNSTAIRS_SWITCH, STATE_OFF)
    hass.states.async_set(UPSTAIRS_SENSOR, "40", {"device_class": "temperature"})
    hass.states.async_set(DOWNSTAIRS_SENSOR, "40", {"device_class": "temperature"})

    calls = {"turn_on": [], "turn_off": []}

    def _targets(call):
        entity_id = call.data.get("entity_id")
        if entity_id is None:
            return []
        return [entity_id] if isinstance(entity_id, str) else list(entity_id)

    async def _turn_on(call):
        calls["turn_on"].append(call)
        for entity_id in _targets(call):
            hass.states.async_set(entity_id, STATE_ON)

    async def _turn_off(call):
        calls["turn_off"].append(call)
        for entity_id in _targets(call):
            hass.states.async_set(entity_id, STATE_OFF)

    hass.services.async_register("homeassistant", "turn_on", _turn_on)
    hass.services.async_register("homeassistant", "turn_off", _turn_off)
    return calls


async def setup_both(hass, *, budget_w=0.0, stagger_seconds=60.0, **entry_kwargs):
    """Load both config entries and return them."""
    upstairs = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, 2000.0, budget_w, stagger_seconds,
        **entry_kwargs,
    )
    downstairs = build_entry(
        "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR, 1300.0, budget_w, stagger_seconds,
        **entry_kwargs,
    )
    for entry in (upstairs, downstairs):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return upstairs, downstairs


def commanded(calls):
    """Return the entity ids that were actually commanded."""
    return [call.data.get("entity_id") for call in calls]


async def test_shared_eco_trigger_switches_on_one_heater_at_a_time(hass, world):
    """The whole incident: one trigger, two instances, one switch command."""
    await setup_both(hass)
    world["turn_on"].clear()

    with freeze_time(TRIGGER):
        # Battery hits 100%, PV excess turns on, both template listeners fire in
        # the same event-loop pass.
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

    assert len(commanded(world["turn_on"])) == 1, (
        f"both heaters stepped on at once: {commanded(world['turn_on'])}"
    )
    assert async_get_fleet(hass).committed_power_w in (2000.0, 1300.0)


async def test_shared_eco_trigger_respects_the_watt_budget(hass, world):
    """Nameplate admission holds even with staggering disabled."""
    await setup_both(hass, budget_w=2500.0, stagger_seconds=0.0)
    world["turn_on"].clear()

    with freeze_time(TRIGGER) as frozen:
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

        # Let the arbitration window close and both instances retry.
        for _ in range(3):
            frozen.tick(timedelta(seconds=2))
            for entry_id in list(hass.data[DOMAIN]):
                if entry_id == FLEET_KEY:
                    continue
                entity = hass.data[DOMAIN][entry_id].get("water_heater_entity")
                if entity is not None:
                    await entity._async_control_heating()
            await hass.async_block_till_done()

    fleet = async_get_fleet(hass)
    assert fleet.committed_power_w <= 2500.0
    assert len(set(commanded(world["turn_on"]))) == 1


async def test_unloading_one_entry_leaves_the_other_running(hass, world):
    """The shared fleet must survive an individual entry unload."""
    upstairs, downstairs = await setup_both(hass, budget_w=2500.0)
    fleet = async_get_fleet(hass)
    assert fleet.get(upstairs.entry_id) is not None
    assert fleet.get(downstairs.entry_id) is not None

    assert await hass.config_entries.async_unload(upstairs.entry_id)
    await hass.async_block_till_done()

    assert fleet.get(upstairs.entry_id) is None
    assert fleet.get(downstairs.entry_id) is not None
    assert not fleet.is_empty
    # The surviving entry keeps its runtime, and the fleet is still reachable.
    assert downstairs.entry_id in hass.data[DOMAIN]
    assert hass.data[DOMAIN][FLEET_KEY] is fleet
    assert upstairs.entry_id not in hass.data[DOMAIN]


async def test_unloading_every_entry_cleans_up_the_shared_object(hass, world):
    """No stale fleet is left behind once the last entry goes."""
    upstairs, downstairs = await setup_both(hass)

    for entry in (upstairs, downstairs):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert FLEET_KEY not in hass.data.get(DOMAIN, {})


async def test_options_update_reloads_without_losing_the_fleet(hass, world):
    """Changing nominal power must land on the fleet, not orphan the entry."""
    upstairs, _ = await setup_both(hass)

    hass.config_entries.async_update_entry(
        upstairs, options={**upstairs.data, CONF_NOMINAL_POWER_W: 2500.0}
    )
    await hass.async_block_till_done()

    fleet = async_get_fleet(hass)
    member = fleet.get(upstairs.entry_id)
    assert member is not None
    assert member.nominal_power_w == 2500.0


def entities(hass):
    """Return the live water heater entities, keyed by name."""
    return {
        runtime["water_heater_entity"].name: runtime["water_heater_entity"]
        for key, runtime in hass.data[DOMAIN].items()
        if key != FLEET_KEY and "water_heater_entity" in runtime
    }


def split_by_hold(hass):
    """Return (admitted, deferred) entities after a contended trigger."""
    live = list(entities(hass).values())
    deferred = [e for e in live if e._fleet_hold_reason is not None]
    admitted = [e for e in live if e._fleet_hold_reason is None]
    assert len(deferred) == 1 and len(admitted) == 1, (
        f"expected exactly one held heater, got {[e._fleet_hold_reason for e in live]}"
    )
    return admitted[0], deferred[0]


async def test_physical_switch_on_during_a_fleet_hold_is_still_honoured(hass, world):
    """A human at the wall beats the fleet: their ON stands and is not undone."""
    await setup_both(hass)

    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

    admitted, deferred = split_by_hold(hass)
    assert deferred._pending_switch_state == STATE_ON
    assert deferred._cooldown_timer is not None
    world["turn_on"].clear()
    world["turn_off"].clear()

    # The human walks up and flips the held heater's switch on by hand.
    with freeze_time(TRIGGER + timedelta(seconds=10)):
        hass.states.async_set(deferred.heater_entity_id, STATE_ON)
        await hass.async_block_till_done()

    # The integration must not fight them.
    assert commanded(world["turn_off"]) == [], "the integration undid a human's switch-on"
    # The stale retry was dropped, and Smart Eco stepped back for the human.
    assert deferred._cooldown_timer is None
    assert deferred._smart_eco_pause_reason is not None
    assert not deferred._is_smart_eco_enforcing()
    # And the watts they just added are visible to the other heater.
    fleet = async_get_fleet(hass)
    member = fleet.get(deferred._entry_id)
    assert member.committed is True
    assert fleet.committed_power_w == 3300.0


async def test_physical_switch_off_is_not_undone_by_the_fleet(hass, world):
    """A human's OFF must stick: no retry or sibling wake may reverse it."""
    await setup_both(hass)

    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

    admitted, deferred = split_by_hold(hass)
    fleet = async_get_fleet(hass)
    assert fleet.get(admitted._entry_id).committed is True
    world["turn_on"].clear()

    # The human switches the running heater off at the wall.
    with freeze_time(TRIGGER + timedelta(seconds=10)):
        hass.states.async_set(admitted.heater_entity_id, STATE_OFF)
        await hass.async_block_till_done()

    assert admitted.heater_entity_id not in commanded(world["turn_on"]), (
        "the fleet turned a manually switched-off heater back on"
    )
    assert admitted._current_operation == STATE_OFF
    assert admitted._smart_eco_pause_reason is not None
    # Its watts went back to the pool rather than being held by a dead commitment.
    assert fleet.get(admitted._entry_id).committed is False


async def test_a_manual_switch_on_is_never_refused_only_accounted(hass, world):
    """The fleet can delay its own commands; it can never veto a human."""
    await setup_both(hass, budget_w=100.0)  # absurdly tight, refuses everything
    fleet = async_get_fleet(hass)

    with freeze_time(TRIGGER):
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        hass.states.async_set(DOWNSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()

    # Both are booked well past the budget: accounting follows reality rather
    # than pretending load the fleet did not authorise is not there.
    assert fleet.committed_power_w == 3300.0
    assert fleet.committed_power_w > fleet.budget_w
    assert commanded(world["turn_off"]) == []


async def test_killing_a_heater_at_the_breaker_eventually_frees_its_sibling(hass, world):
    """A heater cut off upstream must not starve the fleet indefinitely.

    Its watts are held at first -- an unavailable switch may still be drawing --
    but the far likelier cause is that the element lost power, so the claim has
    to lapse rather than block every sibling for ever.
    """
    await setup_both(hass, budget_w=2500.0)
    fleet = async_get_fleet(hass)

    with freeze_time(TRIGGER) as frozen:
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        for _ in range(2):
            frozen.tick(timedelta(seconds=2))
            for entity in entities(hass).values():
                await entity._async_control_heating()
            await hass.async_block_till_done()

        admitted, deferred = split_by_hold(hass)
        assert fleet.committed_power_w == admitted._nominal_power_w
        world["turn_on"].clear()

        # Someone kills that heater at the breaker: the switch stops reporting.
        frozen.tick(timedelta(seconds=5))
        hass.states.async_set(admitted.heater_entity_id, STATE_UNAVAILABLE)
        await hass.async_block_till_done()

        # Still held: it might genuinely still be drawing.
        assert fleet.committed_power_w == admitted._nominal_power_w
        frozen.tick(timedelta(seconds=60))
        await deferred._async_control_heating()
        await hass.async_block_till_done()
        assert commanded(world["turn_on"]) == []

        # Once the claim lapses the sibling can heat again.
        frozen.tick(timedelta(seconds=UNAVAILABLE_COMMITMENT_SECONDS + 10))
        await deferred._async_control_heating()
        await hass.async_block_till_done()

    assert commanded(world["turn_on"]) == [deferred.heater_entity_id]
    assert fleet.committed_power_w == deferred._nominal_power_w


# ---------------------------------------------------------------------------
# Regression: the 2026-09-08 runaway
#
# A Wi-Fi dropout on the switch returned as unavailable -> on, which the manual
# override handling read as a person flipping it. Because the tank was already
# above target - cold_tolerance, the "they want heat now" rule promoted ELECTRIC
# to PERFORMANCE -- which ignores the target and runs the element to the tank's
# own mechanical cutout. Two evenings, 63.7 C and 64.4 C against a 45 C target,
# roughly 5 kWh off the house battery each time.
#
# The real trace contains BOTH outcomes 21 minutes apart, which is what makes it
# such a good test case: the flap below the threshold was harmless, the one
# above it was not.
# ---------------------------------------------------------------------------

REAL_TARGET = 45.0
REAL_COLD_TOLERANCE = 2.0   # electric wants heat at or below 43 C
REAL_HOT_TOLERANCE = 3.0    # and gives up at or above 48 C


async def heating_upstairs(hass, world, temperature):  # noqa: F811
    """Get the upstairs tank heating under eco at a chosen temperature."""
    hass.states.async_set(UPSTAIRS_SENSOR, str(temperature), {"device_class": "temperature"})
    await setup_both(
        hass,
        target_temp=REAL_TARGET,
        cold_tolerance=REAL_COLD_TOLERANCE,
        hot_tolerance=REAL_HOT_TOLERANCE,
    )
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    return entities(hass)["Upstairs"]


async def flap(hass, entity_id):
    """Drop the switch off the network and let it come back on, as observed."""
    hass.states.async_set(entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, STATE_ON)
    await hass.async_block_till_done()


async def test_dropout_above_the_threshold_must_not_promote_via_pending(hass, world):
    """The 16:31 flap, through the branch that actually fired.

    The promotion needs an outstanding deferred command -- in production the
    fleet stagger or a min_off/min_on cooldown arms it, and the real incident
    had one outstanding. Without that precondition neither override branch fires
    and the bug does not reproduce at all, so it is set explicitly here.
    """
    upstairs = await heating_upstairs(hass, world, 41.0)
    hass.states.async_set(UPSTAIRS_SENSOR, "47", {"device_class": "temperature"})
    await hass.async_block_till_done()
    assert not upstairs._electric_mode_wants_heating(), "test premise: electric would idle"
    assert upstairs.state == STATE_ELECTRIC

    upstairs._pending_switch_state = STATE_ON
    await flap(hass, UPSTAIRS_SWITCH)

    assert upstairs.state != STATE_PERFORMANCE, (
        "a network dropout promoted the tank to performance and would run the "
        "element to its mechanical cutout"
    )


async def test_dropout_must_not_promote_via_stale_command_baseline(hass, world):
    """The other override branch: the switch reappears disagreeing with us.

    If the integration last commanded OFF and the device comes back reporting
    ON, that mismatch is a reconnect reporting its own state -- not a person.
    """
    upstairs = await heating_upstairs(hass, world, 41.0)
    hass.states.async_set(UPSTAIRS_SENSOR, "47", {"device_class": "temperature"})
    await hass.async_block_till_done()
    assert not upstairs._electric_mode_wants_heating()

    upstairs._last_commanded_switch_state = STATE_OFF
    await flap(hass, UPSTAIRS_SWITCH)

    assert upstairs.state != STATE_PERFORMANCE, (
        "a reconnect disagreeing with the last command was read as a human"
    )


async def test_a_real_flip_above_the_threshold_still_promotes(hass, world):
    """The feature itself is preserved: a genuine off -> on still means heat now."""
    upstairs = await heating_upstairs(hass, world, 41.0)
    hass.states.async_set(UPSTAIRS_SENSOR, "47", {"device_class": "temperature"})
    await hass.async_block_till_done()

    # A person at the switch device's own button: it stays powered, so this is
    # a real off -> on, not a reappearance.
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
    await hass.async_block_till_done()
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    await hass.async_block_till_done()

    assert upstairs.state == STATE_PERFORMANCE, (
        "a genuine physical flip should still force heat"
    )
