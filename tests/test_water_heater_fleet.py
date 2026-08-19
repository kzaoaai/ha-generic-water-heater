"""Entity-level tests for fleet admission through the real choke point.

test_fleet.py tests the decision logic in isolation. These tests drive two real
GenericWaterHeater entities on a real Home Assistant event loop and assert on
the service calls that actually reach the switches -- the thing that tripped the
inverter on 2026-08-18.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.generic_water_heater import DOMAIN, async_get_fleet
from custom_components.generic_water_heater.fleet import (
    DEPARTED_COMMITMENT_SECONDS,
    FLEET_KEY,
)
from custom_components.generic_water_heater.water_heater import GenericWaterHeater

UPSTAIRS_ENTRY = "01JQ0000000000000000UPSTRS"
DOWNSTAIRS_ENTRY = "01JQ0000000000000000DWNSTR"

UPSTAIRS_SWITCH = "switch.upstairs_element"
DOWNSTAIRS_SWITCH = "switch.downstairs_element"
UPSTAIRS_SENSOR = "sensor.upstairs_water_temperature"
DOWNSTAIRS_SENSOR = "sensor.downstairs_water_temperature"

# 13:51:26.785, when the PV-excess sensor turned on and both instances fired.
TRIGGER = datetime(2026, 8, 18, 13, 51, 26, 785000, tzinfo=timezone.utc)


@pytest.fixture
def switches(hass):
    """Put both switches and sensors in the state machine, with live services.

    The service handlers actually move the switch state, the way a real switch
    does, so an already-on heater is not commanded a second time.
    """
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
    hass.states.async_set(DOWNSTAIRS_SWITCH, STATE_OFF)
    hass.states.async_set(UPSTAIRS_SENSOR, "40")
    hass.states.async_set(DOWNSTAIRS_SENSOR, "40")

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


@pytest.fixture
def make_heater(hass):
    """Return a factory for heater entities wired to the shared fleet."""
    built = []

    def _make(
        entry_id,
        name,
        switch_entity,
        sensor_entity,
        *,
        current_temp=40.0,
        target_temp=60.0,
        nominal_power_w=0.0,
        stagger_seconds=60.0,
        budget_w=0.0,
    ):
        heater = GenericWaterHeater(
            hass,
            name,
            switch_entity,
            sensor_entity,
            target_temp,
            1.0,
            0.0,
            0.0,
            15.0,
            80.0,
            timedelta(seconds=0),
            timedelta(seconds=0),
            None,
            False,
            "°C",
            {},
            6,
            config_entry_id=entry_id,
            nominal_power_w=nominal_power_w,
            fleet_stagger_seconds=stagger_seconds,
            fleet_power_budget_w=budget_w,
        )
        heater.entity_id = f"water_heater.{name.lower()}"
        heater._current_temperature = current_temp
        heater._current_operation = STATE_ELECTRIC
        # What async_added_to_hass does for real. These tests drive the choke
        # point directly, so they register the same way it would.
        async_get_fleet(hass).register(
            entry_id,
            name,
            nominal_power_w=nominal_power_w,
            stagger_seconds=stagger_seconds,
            budget_w=budget_w,
            wake=heater._async_fleet_wake,
        )
        built.append(heater)
        return heater

    yield _make

    for heater in built:
        if heater._cooldown_timer:
            heater._cooldown_timer()
            heater._cooldown_timer = None


def commanded(calls):
    """Return the entity ids that were actually commanded."""
    return [call.data.get("entity_id") for call in calls]


async def test_simultaneous_eco_trigger_commands_only_one_switch(
    hass, switches, make_heater
):
    """The incident, replayed: both instances fire, only one switch is commanded."""
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        nominal_power_w=2000.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        nominal_power_w=1300.0,
    )

    with freeze_time(TRIGGER) as frozen:
        # One shared eco template fires both listeners in the same pass; the
        # real switches went out 447 ms apart.
        await upstairs._async_heater_turn_on()
        frozen.tick(timedelta(milliseconds=447))
        await downstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH]

    # The blocked instance did not silently give up: it holds a pending ON and a
    # live retry timer on the existing cooldown path.
    assert downstairs._pending_switch_state == STATE_ON
    assert downstairs._cooldown_timer is not None
    assert "stagger" in downstairs._fleet_hold_reason


async def test_stagger_expiry_admits_the_second_switch(hass, switches, make_heater):
    """A staggered heater is delayed, never dropped."""
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        nominal_power_w=2000.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        nominal_power_w=1300.0,
    )

    with freeze_time(TRIGGER) as frozen:
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH]

        frozen.tick(timedelta(seconds=61))
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await downstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH, DOWNSTAIRS_SWITCH]
    assert downstairs._fleet_hold_reason is None


async def test_budget_refuses_the_second_switch(hass, switches, make_heater):
    """With staggering off, the nameplate budget alone still stops the step."""
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        nominal_power_w=2000.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        nominal_power_w=1300.0, stagger_seconds=0.0, budget_w=2500.0,
    )

    with freeze_time(TRIGGER) as frozen:
        # A budget too small for both opens the arbitration window, so the first
        # pass admits nobody.
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        assert commanded(switches["turn_on"]) == []

        frozen.tick(timedelta(seconds=2))
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH]
    assert "budget" in downstairs._fleet_hold_reason
    assert async_get_fleet(hass).committed_power_w == 2000.0


async def test_colder_tank_wins_the_only_free_slot(hass, switches, make_heater):
    """Priority: the heater with the larger deficit takes the single slot."""
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        current_temp=59.0,  # 1 degree below target
        nominal_power_w=2000.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        current_temp=30.0,  # 30 degrees below target
        nominal_power_w=1300.0, stagger_seconds=0.0, budget_w=2500.0,
    )

    with freeze_time(TRIGGER) as frozen:
        await upstairs._async_heater_turn_on()
        frozen.tick(timedelta(milliseconds=447))
        await downstairs._async_heater_turn_on()

        # Upstairs asks again first, but yields to the colder tank and wakes it.
        frozen.tick(timedelta(seconds=2))
        await upstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert commanded(switches["turn_on"]) == [DOWNSTAIRS_SWITCH]
    assert "priority" in upstairs._fleet_hold_reason


async def test_turning_one_heater_off_wakes_and_admits_the_sibling(
    hass, switches, make_heater
):
    """A release must hand capacity straight to the waiting heater."""
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        nominal_power_w=2000.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        nominal_power_w=1300.0, stagger_seconds=0.0, budget_w=2500.0,
    )

    with freeze_time(TRIGGER) as frozen:
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        frozen.tick(timedelta(seconds=2))
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH]

        frozen.tick(timedelta(seconds=2))
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await upstairs._async_heater_turn_off()
        # No second request from the test: the wake alone must drive it.
        await hass.async_block_till_done()

    assert commanded(switches["turn_off"]) == [UPSTAIRS_SWITCH]
    assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH, DOWNSTAIRS_SWITCH]
    assert async_get_fleet(hass).committed_power_w == 1300.0


async def test_unloading_one_entry_holds_its_watts_then_frees_them(
    hass, switches, make_heater
):
    """An unloaded entry does not switch its heater off, so hold its watts.

    A config entry reload is what an options save does, and it is exactly when
    both elements could otherwise end up on together.
    """
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        nominal_power_w=2000.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        nominal_power_w=1300.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    fleet = async_get_fleet(hass)

    with freeze_time(TRIGGER) as frozen:
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        frozen.tick(timedelta(seconds=2))
        await upstairs._async_heater_turn_on()
        await downstairs._async_heater_turn_on()
        assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH]
        assert fleet.committed_power_w == 2000.0

        # What entity removal does when the config entry unloads.
        frozen.tick(timedelta(seconds=2))
        upstairs._async_fleet_unregister()
        await hass.async_block_till_done()

        assert fleet.get(UPSTAIRS_ENTRY) is None
        assert fleet.get(DOWNSTAIRS_ENTRY) is not None, "the fleet must survive one unload"
        # No sibling is invited in while the departed element may still be live.
        assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH]
        assert fleet.committed_power_w == 2000.0

        # Once the grace lapses the survivor takes the capacity on its own retry.
        frozen.tick(timedelta(seconds=DEPARTED_COMMITMENT_SECONDS + 1))
        await downstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH, DOWNSTAIRS_SWITCH]
    assert fleet.committed_power_w == 1300.0


async def test_fleet_is_shared_and_keyed_out_of_the_way(hass):
    """Both entries must coordinate through one object under a reserved key."""
    fleet = async_get_fleet(hass)

    assert async_get_fleet(hass) is fleet
    assert hass.data[DOMAIN][FLEET_KEY] is fleet
    # Config entry ids are 26-character ULIDs, so the reserved key cannot be one.
    assert FLEET_KEY != UPSTAIRS_ENTRY and len(FLEET_KEY) < 26


async def test_a_switch_already_on_is_booked_before_a_sibling_is_admitted(
    hass, switches, make_heater
):
    """Load already on the inverter must be counted, not asked permission for.

    After a restart or a reload the fleet has no memory of what is running, so a
    control pass on an already-on heater has to book its watts before any
    sibling is allowed to stack on top of them.
    """
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        nominal_power_w=2000.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
        nominal_power_w=1300.0, stagger_seconds=0.0, budget_w=2500.0,
    )
    fleet = async_get_fleet(hass)

    # Upstairs is already drawing, but nothing ever admitted it.
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    assert fleet.committed_power_w == 0.0

    with freeze_time(TRIGGER) as frozen:
        await upstairs._async_heater_turn_on()
        assert fleet.committed_power_w == 2000.0, "running load must be booked"

        frozen.tick(timedelta(seconds=2))
        await downstairs._async_heater_turn_on()
        frozen.tick(timedelta(seconds=2))
        await downstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert commanded(switches["turn_on"]) == []
    assert "budget" in downstairs._fleet_hold_reason
