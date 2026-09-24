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
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.generic_water_heater import (
    CONF_COLD_TOLERANCE,
    CONF_ECO_TEMPLATE,
    CONF_FLEET_STAGGER_SECONDS,
    CONF_HEATER,
    CONF_HOT_TOLERANCE,
    CONF_SENSOR,
    CONF_TARGET_TEMP,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DOMAIN,
    async_get_fleet,
)
from custom_components.generic_water_heater.fleet import FLEET_KEY

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
    stagger_seconds=60.0,
    target_temp=60.0,
    cold_tolerance=0.0,
    hot_tolerance=0.0,
    **extra,
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
            CONF_FLEET_STAGGER_SECONDS: stagger_seconds,
            **extra,
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


async def setup_both(hass, *, stagger_seconds=60.0, **entry_kwargs):
    """Load both config entries and return them."""
    upstairs = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, stagger_seconds,
        **entry_kwargs,
    )
    downstairs = build_entry(
        "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR, stagger_seconds,
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


async def test_unloading_one_entry_leaves_the_other_running(hass, world):
    """The shared fleet must survive an individual entry unload."""
    upstairs, downstairs = await setup_both(hass)
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
    """A changed fleet setting must land on the fleet, not orphan the entry."""
    upstairs, _ = await setup_both(hass)

    hass.config_entries.async_update_entry(
        upstairs, options={**upstairs.data, CONF_FLEET_STAGGER_SECONDS: 90.0}
    )
    await hass.async_block_till_done()

    fleet = async_get_fleet(hass)
    member = fleet.get(upstairs.entry_id)
    assert member is not None
    assert member.stagger_seconds == 90.0
    # Resolved fleet-wide to the longest any member asks for.
    assert fleet.stagger_seconds == 90.0


async def test_an_options_save_does_not_clear_the_stagger_window(hass, world):
    """A real reload is unregister THEN register, which pops the member.

    So state kept on the member object cannot survive it. This drives the whole
    reload through Home Assistant rather than calling register twice, which is
    how the first version of this test managed to pass against the bug.
    """
    upstairs, downstairs = await setup_both(hass)
    fleet = async_get_fleet(hass)

    with freeze_time(TRIGGER) as frozen:
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        admitted, deferred = split_by_hold(hass)
        anchor_before = fleet.get(admitted._entry_id).last_admitted
        assert anchor_before is not None

        # Save options on the heater that was admitted: HA unloads and reloads it.
        frozen.tick(timedelta(seconds=2))
        entry = next(
            e for e in (upstairs, downstairs) if e.entry_id == admitted._entry_id
        )
        hass.config_entries.async_update_entry(
            entry, options={**entry.data, CONF_FLEET_STAGGER_SECONDS: 60.0}
        )
        await hass.async_block_till_done()

    member = fleet.get(entry.entry_id)
    assert member is not None, "the reloaded entry never rejoined the fleet"
    assert member.last_admitted == anchor_before, (
        "the reload cleared the stagger window, so a sibling could step on"
    )


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


async def test_physical_switch_off_is_not_undone_by_the_fleet(hass, world):
    """A human's OFF must stick: no deferred retry may reverse it."""
    await setup_both(hass)

    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

    admitted, deferred = split_by_hold(hass)
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


async def test_an_entry_upgraded_from_1_x_still_loads(hass, world):
    """2.0.0 removed two options; real entries out there still contain them.

    This is a published integration, so every existing config entry carries
    nominal_power_w and fleet_power_budget_w in its data. Nothing reads them any
    more, but "nothing reads them" has to mean the entry loads, the platforms
    come up, and the fleet still staggers -- not that setup raises on a key the
    schema no longer declares.
    """
    stale = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, 60.0,
        nominal_power_w=2100.0,
        fleet_power_budget_w=3000.0,
    )
    stale.add_to_hass(hass)
    assert await hass.config_entries.async_setup(stale.entry_id)
    await hass.async_block_till_done()

    fleet = async_get_fleet(hass)
    member = fleet.get(stale.entry_id)
    assert member is not None, "an upgraded entry never joined the fleet"
    assert member.stagger_seconds == 60.0

    # The dead keys are inert, not authoritative: no attribute resurrects them.
    entity = hass.data[DOMAIN][stale.entry_id]["water_heater_entity"]
    attrs = entity.extra_state_attributes
    assert "nominal_power_w" not in attrs
    assert "fleet_power_budget_w" not in attrs
    assert "fleet_committed_power_w" not in attrs
    assert attrs["fleet_stagger_seconds"] == 60.0


async def test_the_options_form_ignores_the_removed_keys(hass, world):
    """Rendering the options form for an upgraded entry must not raise.

    _build_data_schema reads the current values key by key, so a key it no
    longer knows about has to be simply unread -- and the two removals must not
    disturb any option that survives.
    """
    stale = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, 90.0,
        nominal_power_w=2100.0,
        fleet_power_budget_w=3000.0,
    )
    stale.add_to_hass(hass)
    assert await hass.config_entries.async_setup(stale.entry_id)
    await hass.async_block_till_done()

    flow = await hass.config_entries.options.async_init(stale.entry_id)
    assert flow["type"] == "form"
    schema_keys = {str(k.schema) for k in flow["data_schema"].schema}
    assert "fleet" in schema_keys
    # The eco template is the one that matters: it has no schema default, only a
    # suggested value, so a form that dropped it would disable Smart Eco.
    assert "smart_eco" in schema_keys


# ---------------------------------------------------------------------------
# Observed switch state and the fleet: what is pinned, and what is not
#
# fleet.py's note_switch_on / note_switch_off semantics are covered in
# test_fleet.py. The three WIRING sites in water_heater.py that call them are
# deliberately NOT claimed to be covered here, and an honest account of why is
# worth more than a test that looks like coverage:
#
#   * Removing all three leaves this suite green. That is not laziness in the
#     tests -- it is that a heater which is drawing normally anchors itself. If
#     the integration wants the element on, its own control pass calls
#     request_turn_on and is admitted, which opens the window; if it does not
#     want it on, it turns the switch off. Either way the wiring is not what
#     produces the observable outcome.
#   * What the wiring actually buys is the in-pass ordering race: two entities
#     run their control passes in one event-loop pass, and without it the one
#     that asks first cannot see that its sibling's element is ALREADY on. That
#     is the 2026-08-18 shape, and it is sub-millisecond -- there is no way to
#     stage it from a test that awaits async_block_till_done.
#
# So the wiring is kept as the pre-2.0.0 design had it, and this comment is the
# record that it is reasoned about rather than measured. A first attempt at
# "wiring tests" here passed with every call site deleted; deleting them beat
# leaving three tests that implied a guarantee they did not give.
# ---------------------------------------------------------------------------


async def test_a_deferred_sibling_still_gets_its_turn_after_the_window(hass, world):
    """Whatever opened the window, a delay must never become a stranding."""
    await setup_both(hass)
    world["turn_on"].clear()

    with freeze_time(TRIGGER) as frozen:
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        admitted, deferred = split_by_hold(hass)
        assert deferred.heater_entity_id not in commanded(world["turn_on"])

        frozen.tick(timedelta(seconds=61))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert deferred.heater_entity_id in commanded(world["turn_on"]), (
        "the deferred sibling never got its turn"
    )
    assert deferred._fleet_hold_reason is None
