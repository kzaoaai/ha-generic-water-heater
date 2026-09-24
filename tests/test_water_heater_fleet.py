"""Entity-level tests for switch-on staggering through the real choke point.

test_fleet.py tests the decision logic in isolation. These tests drive two real
GenericWaterHeater entities on a real Home Assistant event loop and assert on
the service calls that actually reach the switches -- the thing that tripped the
inverter on 2026-08-18.

Nameplate-watt admission was removed in 2.0.0 (the Power Load Balancer owns
power balancing), so what is left to prove here is sequencing plus the one piece
of budget behaviour that moved OUT of fleet.py: a heater that is already drawing
is not asked for permission at all.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.generic_water_heater import DOMAIN, async_get_fleet
from custom_components.generic_water_heater.fleet import FLEET_KEY
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
        stagger_seconds=60.0,
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
            fleet_stagger_seconds=stagger_seconds,
        )
        heater.entity_id = f"water_heater.{name.lower()}"
        heater._current_temperature = current_temp
        heater._current_operation = STATE_ELECTRIC
        # What async_added_to_hass does for real. These tests drive the choke
        # point directly, so they register the same way it would.
        async_get_fleet(hass).register(
            entry_id,
            name,
            stagger_seconds=stagger_seconds,
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
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
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
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
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


async def test_fleet_is_shared_and_keyed_out_of_the_way(hass):
    """Both entries must coordinate through one object under a reserved key."""
    fleet = async_get_fleet(hass)

    assert async_get_fleet(hass) is fleet
    assert hass.data[DOMAIN][FLEET_KEY] is fleet
    # Config entry ids are 26-character ULIDs, so the reserved key cannot be one.
    assert FLEET_KEY != UPSTAIRS_ENTRY and len(FLEET_KEY) < 26


# ---------------------------------------------------------------------------
# Configured minimum durations
# ---------------------------------------------------------------------------


async def test_a_configured_zero_minimum_is_honoured(hass, switches, make_heater):
    """timedelta(0) is falsy, and was being mistaken for "not configured".

    The config said 0 and the code used 120 s, which is the worst kind of
    disagreement: silent, and only visible as a command that mysteriously
    does not land for two minutes.
    """
    heater = make_heater(UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR)

    assert heater._min_off_duration == timedelta(0)
    assert heater._min_on_duration == timedelta(0)


async def test_an_omitted_minimum_still_gets_its_default(hass, switches):
    """Absent means "use the default" -- only absent."""
    heater = GenericWaterHeater(
        hass, "Defaults", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, 60.0, 1.0, 0.0, 0.0,
        15.0, 80.0,
        None,  # min_on_duration omitted
        None,  # min_off_duration omitted
        None, False, "°C", {}, 6,
        config_entry_id="01JQ000000000000000DEFLT",
    )

    assert heater._min_on_duration == timedelta(seconds=0)
    assert heater._min_off_duration == timedelta(seconds=120)


async def test_a_heater_already_drawing_is_never_given_a_stagger_hold(
    hass, switches, make_heater
):
    """A running heater's own control passes must not defer to its sibling.

    The first attempt at this exemption put it at the caller -- skip the fleet
    entirely when the switch reads ON. That was wrong in both directions: it
    never opened the running heater's own window (so a sibling could step onto
    it), and it keyed off literal switch state, so a switch that stopped
    reporting while still drawing lost the exemption. It now works the way the
    pre-2.0.0 code did: tell the fleet the element is drawing, which both opens
    its window and makes the request a free re-affirmation.
    """
    upstairs = make_heater(
        UPSTAIRS_ENTRY, "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
    )
    downstairs = make_heater(
        DOWNSTAIRS_ENTRY, "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR,
    )

    with freeze_time(TRIGGER) as frozen:
        # Upstairs goes on and its switch reports ON, as a real one would.
        await upstairs._async_heater_turn_on()
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()

        # Well past the stagger: downstairs is admitted and reports ON too.
        frozen.tick(timedelta(seconds=61))
        await downstairs._async_heater_turn_on()
        hass.states.async_set(DOWNSTAIRS_SWITCH, STATE_ON)
        await hass.async_block_till_done()
        assert commanded(switches["turn_on"]) == [UPSTAIRS_SWITCH, DOWNSTAIRS_SWITCH]

        upstairs._fleet_hold_reason = None
        switches["turn_on"].clear()

        # A routine control pass on the still-running upstairs heater, one
        # second after its sibling was admitted -- squarely inside the window.
        frozen.tick(timedelta(seconds=1))
        await upstairs._async_heater_turn_on()
        await hass.async_block_till_done()

    assert upstairs._fleet_hold_reason is None, (
        "a running heater was told it was waiting on a stagger"
    )
    assert upstairs._cooldown_timer is None, "a pointless retry was armed"
    # Nothing needed commanding: it was already on.
    assert commanded(switches["turn_on"]) == []
