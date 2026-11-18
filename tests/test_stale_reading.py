"""A reading that does not move while the tank is heated is a lost reading.

Seen 2026-11-18: a battery sensor behind a bridge made its last report at
05:18 UTC. The bridge went on serving that number, and replayed it after every
Home Assistant restart, so the entity stayed available with a numeric state
and the failsafe had nothing to see. From 10:35 the element heated for about
90 minutes on it.

The rule: once the tank has been heated for STALE_READING_AFTER since its
reading last moved by STALE_READING_BAND, the reading is held as lost until it
moves or a person asks for heat. "Heated" is the relay on, plus either a tank
too cold for a mechanical thermostat to be open, or a meter showing the draw.

The count itself (HeatingWatch) is pinned in test_reading_health.py.
"""

from datetime import datetime, timedelta, timezone
import json
import logging

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.json import JSONEncoder
import homeassistant.util.dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.generic_water_heater import (
    CONF_ECO_TEMPLATE,
    CONF_SENSOR,
    DOMAIN,
    SMART_ECO_MODE_ALWAYS_ON,
)
from custom_components.generic_water_heater.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.generic_water_heater.water_heater import (
    STALE_READING_AFTER,
    STALE_READING_BAND,
    HeaterStoredData,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    entities,
    world,
)

UPSTAIRS = "water_heater.upstairs"
METER = "sensor.element_meter"
T0 = datetime(2026, 11, 18, 10, 35, tzinfo=timezone.utc)
STALE = "temperature_sensor_stale"


def test_the_numbers():
    """Pinned: the README and the repair text state them."""
    assert STALE_READING_AFTER == timedelta(minutes=30)
    assert STALE_READING_BAND == 0.5


def heater(hass):
    return hass.states.get(UPSTAIRS)


def relay(hass):
    return hass.states.get(UPSTAIRS_SWITCH).state


def stale(hass):
    return heater(hass).attributes["stale_temperature"]


def issue(hass, kind, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


def tank(hass, value):
    hass.states.async_set(UPSTAIRS_SENSOR, value, {"device_class": "temperature"})


def meter(hass, value, unit="W"):
    hass.states.async_set(
        METER, value, {"unit_of_measurement": unit, "device_class": "power"}
    )


async def tick(hass, frozen, minutes):
    frozen.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def heating(hass, *, temperature="40", target=60.0, metered=None, **extra):
    """One heater with no eco condition. Target 60, tank at 40: it heats.

    40 is in the cold region (at or below min(target, 50) - 5), where the relay
    alone counts as heating. With `metered`, a meter is configured and reads
    that.
    """
    tank(hass, temperature)
    if metered is not None:
        meter(hass, metered)
        extra["power_sensor"] = METER
    entry = build_entry(
        "Upstairs",
        UPSTAIRS_SWITCH,
        UPSTAIRS_SENSOR,
        stagger_seconds=0.0,
        target_temp=target,
        **{CONF_ECO_TEMPLATE: "", **extra},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --- the verdict ---------------------------------------------------------------


async def test_a_reading_that_does_not_move_while_the_tank_is_heated_is_lost(
    hass, world  # noqa: F811
):
    with freeze_time(T0) as frozen:
        entry = await heating(hass)
        assert relay(hass) == STATE_ON, "test premise"
        assert stale(hass) is None

        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON
        assert issue(hass, STALE, entry) is None

        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF, "heated 30 minutes on a number that never moved"
        assert stale(hass) == 40
        assert heater(hass).attributes["current_temperature"] is None
        raised = issue(hass, STALE, entry)
        assert raised is not None
        assert raised.translation_placeholders == {
            "name": "Upstairs",
            "entity_id": UPSTAIRS_SENSOR,
            "value": "40",
            "minutes": "30",
        }


async def test_a_reading_that_moves_is_a_live_sensor(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await heating(hass)
        await tick(hass, frozen, 20)
        tank(hass, "40.5")  # exactly the band
        await hass.async_block_till_done()

        await tick(hass, frozen, 29)  # 49 minutes of heat, 29 since the move
        assert relay(hass) == STATE_ON
        assert stale(hass) is None
        # And from the move the count runs again, to the same limit.
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 40.5


async def test_a_move_down_is_a_live_sensor_too(hass, world):  # noqa: F811
    """A tank drawn from harder than it is heated falls while the element is on."""
    with freeze_time(T0) as frozen:
        await heating(hass)
        for _ in range(4):
            await tick(hass, frozen, 20)
            tank(hass, str(float(hass.states.get(UPSTAIRS_SENSOR).state) - 0.5))
            await hass.async_block_till_done()
        assert relay(hass) == STATE_ON
        assert stale(hass) is None


async def test_a_wiggle_inside_the_band_is_not_a_move(hass, world):  # noqa: F811
    """Measured from where the count started, so drifting back and forth is not."""
    with freeze_time(T0) as frozen:
        await heating(hass)
        for value in ("40.2", "40.0", "40.4", "39.7", "40.45"):
            await tick(hass, frozen, 5)
            tank(hass, value)
            await hass.async_block_till_done()
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 5)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 40


async def test_short_runs_add_up(hass, world):  # noqa: F811
    """An eco condition that flaps runs the element a few minutes at a time.

    A count that started again with each run would never end (2026-11-18: six
    3-minute runs in a row).
    """
    with freeze_time(T0) as frozen:
        tank(hass, "40")
        entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, stagger_seconds=0.0)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF, "test premise: parked, the template is false"

        for _ in range(5):
            hass.states.async_set(PV_EXCESS, STATE_ON)
            await hass.async_block_till_done()
            assert relay(hass) == STATE_ON
            await tick(hass, frozen, 5)
            hass.states.async_set(PV_EXCESS, STATE_OFF)
            await hass.async_block_till_done()
            assert relay(hass) == STATE_OFF
            await tick(hass, frozen, 1)

        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        await tick(hass, frozen, 4)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF, "25 minutes banked and 5 more: the limit"
        assert stale(hass) == 40
        assert hass.states.get(PV_EXCESS).state == STATE_ON


async def test_a_tank_that_is_not_heated_is_never_judged(hass, world):  # noqa: F811
    """Silence is not the test: a satisfied tank sits still for hours."""
    with freeze_time(T0) as frozen:
        entry = await heating(hass, temperature="61")
        assert relay(hass) == STATE_OFF, "test premise"
        await tick(hass, frozen, 600)
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None


async def test_a_lost_reading_neither_counts_nor_starts_the_count_again(
    hass, world  # noqa: F811
):
    """The sensor drops out and comes back with the same number: not a move."""
    with freeze_time(T0) as frozen:
        await heating(hass)
        await tick(hass, frozen, 20)
        tank(hass, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF, "the ordinary failsafe"
        await tick(hass, frozen, 60)
        tank(hass, "40")
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON
        assert stale(hass) is None

        await tick(hass, frozen, 9)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF, "20 minutes before the dropout and 10 after"
        assert stale(hass) == 40


async def test_performance_is_judged_like_electric(hass, world):  # noqa: F811
    """Performance needs no temperature to heat, which is the more reason."""
    with freeze_time(T0) as frozen:
        await heating(hass)
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": UPSTAIRS, "operation_mode": STATE_PERFORMANCE},
            blocking=True,
        )
        await hass.async_block_till_done()
        await tick(hass, frozen, 30)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 40
        assert heater(hass).state == STATE_PERFORMANCE, "the mode is the person's"


# --- what counts as heated -------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "temperature", "judged"),
    [
        (60.0, "45", True),  # five below 50
        (60.0, "45.1", False),
        (48.0, "43", True),  # five below the target
        (48.0, "43.1", False),
    ],
)
async def test_without_a_meter_only_a_cold_tank_is_judged(
    hass, world, target, temperature, judged  # noqa: F811
):
    """Above the cold region a closed relay does not prove heat.

    The tank's own thermostat can have opened the circuit, and a tank at its
    cutout sits still for a long time with a silent sensor (seen: 37 minutes
    at 64 C). So a reading stuck up there is not covered without a meter: the
    element runs to the thermostat, as before.
    """
    with freeze_time(T0) as frozen:
        await heating(hass, temperature=temperature, target=target)
        assert relay(hass) == STATE_ON, "test premise"
        await tick(hass, frozen, 120)
        assert (stale(hass) is not None) is judged
        assert relay(hass) == (STATE_OFF if judged else STATE_ON)


async def test_a_meter_that_shows_the_draw_reaches_a_warm_tank(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="47", metered="1190")
        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 47


@pytest.mark.parametrize("reads", ["0.0", "99.9", STATE_UNAVAILABLE, STATE_UNKNOWN, "error"])
async def test_a_warm_tank_whose_meter_shows_no_draw_is_not_judged(
    hass, world, reads  # noqa: F811
):
    """Relay closed, nothing drawn: the tank's thermostat has opened. Not stale."""
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="55", metered=reads)
        assert relay(hass) == STATE_ON, "test premise"
        await tick(hass, frozen, 180)
        assert stale(hass) is None
        assert relay(hass) == STATE_ON


@pytest.mark.parametrize(
    ("value", "unit", "judged"),
    [("1.19", "kW", True), ("0.09", "kW", False), ("100", "W", True), ("100", None, True)],
)
async def test_the_meter_is_read_in_its_unit(hass, world, value, unit, judged):  # noqa: F811
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="55", metered="0")
        hass.states.async_set(
            METER, value, {"unit_of_measurement": unit} if unit else {}
        )
        await hass.async_block_till_done()
        await tick(hass, frozen, 30)
        assert (stale(hass) is not None) is judged


@pytest.mark.parametrize("reads", ["0.0", STATE_UNAVAILABLE])
async def test_a_meter_cannot_hide_a_cold_tank(hass, world, reads):  # noqa: F811
    """The meter only adds. A meter that reads nothing by mistake hides nothing.

    Down in the cold region a closed relay heats or is broken, and stopping is
    right for both.
    """
    with freeze_time(T0) as frozen:
        await heating(hass, metered=reads)
        await tick(hass, frozen, 30)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 40


async def test_the_count_follows_the_meter(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="55", metered="0")
        await tick(hass, frozen, 90)
        assert stale(hass) is None

        meter(hass, "1190")
        await hass.async_block_till_done()
        await tick(hass, frozen, 20)
        meter(hass, "0")  # the thermostat opens
        await hass.async_block_till_done()
        await tick(hass, frozen, 90)
        assert stale(hass) is None, "counted time the element drew nothing"

        meter(hass, "1195")
        await hass.async_block_till_done()
        await tick(hass, frozen, 9)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF, "20 minutes of draw and 10 more"
        assert stale(hass) == 55


async def test_a_meter_report_commands_nothing_and_writes_nothing(hass, world):  # noqa: F811
    """A meter reports every few seconds. It is evidence for the count only."""
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="55", metered="1190")
        before = heater(hass)
        commands = (len(world["turn_on"]), len(world["turn_off"]))
        for value in ("1191.2", "1188.0", "0.0", "1190.4"):
            frozen.tick(timedelta(seconds=20))
            meter(hass, value)
            await hass.async_block_till_done()
        assert heater(hass) is before, "a meter report wrote the heater's state"
        assert (len(world["turn_on"]), len(world["turn_off"])) == commands


async def test_a_new_target_moves_the_cold_region(hass, world):  # noqa: F811
    """No event follows a new target, so setting it refreshes the count."""
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="44", target=48.0)
        await tick(hass, frozen, 90)
        assert stale(hass) is None, "test premise: 44 is above 48 - 5"

        await hass.services.async_call(
            "water_heater", "set_temperature", {"entity_id": UPSTAIRS, "temperature": 60}, blocking=True
        )
        await hass.async_block_till_done()
        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 44


async def test_in_fahrenheit_only_the_meter_judges(hass, world):  # noqa: F811
    """The cold region's bounds are in Celsius and readings are not converted."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="40", target=140.0)
        assert relay(hass) == STATE_ON, "test premise"
        await tick(hass, frozen, 180)
        assert stale(hass) is None


async def test_in_fahrenheit_a_meter_still_judges(hass, world):  # noqa: F811
    hass.config.units = US_CUSTOMARY_SYSTEM
    with freeze_time(T0) as frozen:
        await heating(hass, temperature="104", target=140.0, metered="1190")
        await tick(hass, frozen, 30)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 104


# --- leaving the hold -------------------------------------------------------------


async def held(hass, frozen, **kwargs):
    entry = await heating(hass, **kwargs)
    await tick(hass, frozen, 30)
    assert stale(hass) == 40, "test premise"
    assert relay(hass) == STATE_OFF, "test premise"
    return entry


async def test_the_same_number_again_ends_nothing(hass, world, caplog):  # noqa: F811
    """The bridge replays its cache, or the entity comes back with nothing new."""
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        world["turn_on"].clear()

        tank(hass, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await tick(hass, frozen, 15)
        assert issue(hass, "temperature_sensor_unavailable", entry) is None
        tank(hass, STATE_UNKNOWN)
        await hass.async_block_till_done()
        tank(hass, "40")
        await hass.async_block_till_done()
        assert stale(hass) == 40
        assert relay(hass) == STATE_OFF

        tank(hass, "40.49")  # a stray report inside the band
        await hass.async_block_till_done()
        assert stale(hass) == 40
        assert heater(hass).attributes["current_temperature"] is None
        assert issue(hass, STALE, entry) is not None
        assert world["turn_on"] == []
        await tick(hass, frozen, 600)
        assert relay(hass) == STATE_OFF


async def test_a_reading_that_moved_ends_the_hold(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        tank(hass, "36.5")  # the sensor is back, and the tank has cooled
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert heater(hass).attributes["current_temperature"] == 36.5
        assert relay(hass) == STATE_ON
        # A live sensor again: it is judged from this reading on.
        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON


async def test_the_hold_starts_no_unavailable_repair_and_no_failsafe_warning(
    hass, world, caplog  # noqa: F811
):
    """The sensor is not unavailable, and the hold has its own repair and line."""
    with freeze_time(T0) as frozen:
        with caplog.at_level(logging.WARNING):
            entry = await held(hass, frozen)
            await tick(hass, frozen, 30)
        assert issue(hass, "temperature_sensor_unavailable", entry) is None
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name.startswith("custom_components.")
        ]
        assert len(warnings) == 1, warnings
        assert "has stayed at 40.0 through 30 minutes of heating" in warnings[0]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("set_operation_mode", {"operation_mode": STATE_ELECTRIC}),
        ("set_operation_mode", {"operation_mode": STATE_PERFORMANCE}),
        ("turn_on", {}),
    ],
)
async def test_a_person_asking_for_heat_gives_the_sensor_another_try(
    hass, world, service, data  # noqa: F811
):
    """Otherwise the hold could last for ever.

    It keeps the element off, a tank that is not heated and sits at room
    temperature may not move half a degree for days, and a restart restores
    the hold. The owner who changed the battery could not get heat back.
    """
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        await hass.services.async_call(
            "water_heater", service, {"entity_id": UPSTAIRS, **data}, blocking=True
        )
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert heater(hass).attributes["current_temperature"] == 40
        assert relay(hass) == STATE_ON

        # Another try, not a pardon: the same count, from zero.
        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF
        assert stale(hass) == 40
        assert issue(hass, STALE, entry) is not None


async def test_turn_on_is_asking_for_heat_under_always_on_too(hass, world):  # noqa: F811
    """Always ON ignores turn_on as a mode change. It is still a person asking."""
    with freeze_time(T0) as frozen:
        entry = await heating(hass, **{CONF_ECO_TEMPLATE: "{{ true }}"})
        await entities(hass)["Upstairs"].async_set_smart_eco_mode(
            SMART_ECO_MODE_ALWAYS_ON, source="test"
        )
        await hass.async_block_till_done()
        await tick(hass, frozen, 30)
        assert stale(hass) == 40, "test premise"

        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert relay(hass) == STATE_ON


async def test_a_wild_value_the_filter_is_holding_is_not_taken_at_its_word(hass, world):  # noqa: F811
    """The retry used to read the entity's raw state, past the spike filter.

    Held at 40, the sensor glitches to 75; the filter holds that for 60 s. A
    person asking for heat in that minute got 75 taken as the reading and the
    element left off (a reviewer's repro).
    """
    with freeze_time(T0) as frozen:
        await held(hass, frozen)
        tank(hass, "75")
        await hass.async_block_till_done()
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON
        assert heater(hass).attributes["current_temperature"] == 40


@pytest.mark.parametrize(("stuck", "now"), [("31.8", "32.3"), ("63.6", "64.1"), ("16.4", "15.9")])
async def test_exactly_the_band_in_decimal_readings_ends_a_hold(hass, world, stuck, now):  # noqa: F811
    """32.3 - 31.8 is 0.49999999999999645 in floats."""
    with freeze_time(T0) as frozen:
        await heating(hass, temperature=stuck, target=90.0, metered="1190")
        await tick(hass, frozen, 30)
        assert stale(hass) == float(stuck), "test premise"
        tank(hass, now)
        await hass.async_block_till_done()
        assert stale(hass) is None


async def test_choosing_off_is_not_asking_for_heat(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": UPSTAIRS, "operation_mode": STATE_OFF},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert stale(hass) == 40
        assert issue(hass, STALE, entry) is not None


async def test_asking_for_heat_while_the_sensor_says_nothing_is_a_lost_reading(
    hass, world  # noqa: F811
):
    """There is no number to take at its word: the ordinary failsafe, with its clocks."""
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        tank(hass, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert relay(hass) == STATE_OFF
        await tick(hass, frozen, 11)
        assert issue(hass, "temperature_sensor_unavailable", entry) is not None
        # And the sensor's next number is an ordinary first reading.
        tank(hass, "40")
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON
        assert issue(hass, "temperature_sensor_unavailable", entry) is None


# --- across a restart ---------------------------------------------------------------

HELD = {"temperature": 60, "stale_temperature": 40.0}


def saved(hass, attributes, *, mode=STATE_ELECTRIC, sensor=UPSTAIRS_SENSOR, **stored):
    """What Home Assistant saved for the heater when it went down."""
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(UPSTAIRS, mode, dict(attributes)),
                {
                    "state": mode,
                    "attributes": dict(attributes),
                    "reading_sensor": sensor,
                    **stored,
                },
            ),
        ),
    )


async def test_a_restart_does_not_end_the_hold(hass, world):  # noqa: F811
    """The bridge serves the old number again when Home Assistant comes back.

    Without this each restart bought the sensor another 30 minutes of heat
    (2026-11-18: two restarts that day, the same 28.86 after each).
    """
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        entry = await heating(hass)
        assert stale(hass) == 40
        assert relay(hass) == STATE_OFF
        assert world["turn_on"] == [], "a restored number switched the element on"
        assert heater(hass).attributes["current_temperature"] is None
        assert issue(hass, STALE, entry) is not None, "repairs do not survive a restart"
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["awaiting_first_reading"] is False, "it has its reading: a stale one"
        assert report["timers_armed"]["_first_reading_timer"] is False
        await tick(hass, frozen, 600)
        assert relay(hass) == STATE_OFF


async def test_a_reading_that_moved_over_the_restart_ends_the_hold(hass, world):  # noqa: F811
    saved(hass, HELD)
    with freeze_time(T0):
        entry = await heating(hass, temperature="36.5")
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert relay(hass) == STATE_ON


@pytest.mark.parametrize(
    ("first_number", "still_held"), [("40", True), ("40.3", True), ("36.5", False)]
)
async def test_a_restored_hold_waits_for_a_sensor_still_coming_up(
    hass, world, first_number, still_held  # noqa: F811
):
    """At a start the sensor is unavailable, then unknown, then has a number.

    Nothing is commanded until that number comes, and it is judged against
    the restored one.
    """
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        hass.set_state(CoreState.starting)
        entry = await heating(hass, temperature=STATE_UNAVAILABLE)
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert stale(hass) == 40, "published while it waits, so a second restart keeps it"
        assert world["turn_on"] == [] and world["turn_off"] == []

        await tick(hass, frozen, 1)
        tank(hass, STATE_UNKNOWN)
        await hass.async_block_till_done()
        assert world["turn_on"] == [] and world["turn_off"] == []

        tank(hass, first_number)
        await hass.async_block_till_done()
        assert (stale(hass) is not None) is still_held
        assert (issue(hass, STALE, entry) is not None) is still_held
        assert relay(hass) == (STATE_OFF if still_held else STATE_ON)
        assert bool(world["turn_on"]) is not still_held


async def test_a_restored_hold_whose_sensor_never_speaks_is_an_ordinary_lost_reading(
    hass, world  # noqa: F811
):
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        entry = await heating(hass, temperature=STATE_UNAVAILABLE)
        await tick(hass, frozen, 3)  # the wait for a first reading runs out
        assert stale(hass) == 40, "silence is not a move"
        await tick(hass, frozen, 10)
        assert issue(hass, "temperature_sensor_unavailable", entry) is not None
        assert relay(hass) == STATE_OFF
        assert world["turn_on"] == []

        # It speaks at last, with the number it was stuck on: no longer
        # unavailable, and still stale. One repair goes, the other comes.
        tank(hass, "40")
        await hass.async_block_till_done()
        assert issue(hass, "temperature_sensor_unavailable", entry) is None
        assert issue(hass, STALE, entry) is not None
        assert stale(hass) == 40
        assert world["turn_on"] == []


async def test_asking_for_heat_before_the_sensor_has_spoken_drops_a_restored_hold(
    hass, world  # noqa: F811
):
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        entry = await heating(hass, temperature=STATE_UNAVAILABLE)
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert world["turn_on"] == [], "nothing to heat on yet"
        tank(hass, "40")
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON, "an ordinary first reading"
        assert issue(hass, STALE, entry) is None
        # The sensor was still coming up, not lost: no outage clock was started
        # for it, which nothing would have stopped once it spoke.
        tank(hass, "41")
        await hass.async_block_till_done()
        await tick(hass, frozen, 11)
        assert issue(hass, "temperature_sensor_unavailable", entry) is None


async def test_what_was_learned_about_one_sensor_is_not_held_against_another(
    hass, world  # noqa: F811
):
    """The natural repair for a dead sensor is to configure another one."""
    saved(hass, HELD, sensor="sensor.the_one_that_died")
    with freeze_time(T0):
        entry = await heating(hass)
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert relay(hass) == STATE_ON


async def test_configuring_another_sensor_during_a_hold_clears_the_repair(hass, world):  # noqa: F811
    """The repair's own advice, followed: the dead sensor is replaced in the options.

    The hold is not carried over to the new sensor. Its repair has to go too:
    the issue registry survives a reload, and it went on saying the old sensor
    was holding the heater off while the heater ran on the new one (a
    reviewer's repro).
    """
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        assert issue(hass, STALE, entry) is not None, "test premise"
        hass.states.async_set("sensor.new_tank", "41", {"device_class": "temperature"})
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SENSOR: "sensor.new_tank"}
        )
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert relay(hass) == STATE_ON


async def test_a_reload_during_a_hold_keeps_the_hold_and_its_repair(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert stale(hass) == 40
        assert issue(hass, STALE, entry) is not None
        assert relay(hass) == STATE_OFF


@pytest.mark.parametrize("seconds", [1e300, 9e17, -1e300])
async def test_an_absurd_stored_count_does_not_fail_the_setup(hass, world, seconds):  # noqa: F811
    """A finite number that timedelta() cannot hold (a reviewer's repro)."""
    saved(hass, {"temperature": 60}, watch_value=40.0, watch_heated_s=seconds)
    with freeze_time(T0):
        await heating(hass)
        assert heater(hass) is not None, "the heater failed to load"
        assert relay(hass) == STATE_ON


async def test_a_hold_is_not_restored_without_the_stored_copy(hass, world):  # noqa: F811
    """It names the sensor. With nothing to check that against, nothing is held."""
    mock_restore_cache(hass, (State(UPSTAIRS, STATE_ELECTRIC, dict(HELD)),))
    with freeze_time(T0):
        await heating(hass)
        assert stale(hass) is None
        assert relay(hass) == STATE_ON


@pytest.mark.parametrize("junk", [True, "40", [40], {"a": 1}, float("nan"), float("inf")])
async def test_a_stale_value_that_is_not_a_number_is_not_restored(hass, world, junk):  # noqa: F811
    saved(hass, {"temperature": 60, "stale_temperature": junk})
    with freeze_time(T0):
        await heating(hass)
        assert stale(hass) is None
        assert relay(hass) == STATE_ON


async def test_true_is_not_the_number_one(hass, world):  # noqa: F811
    """float(True) is 1.0: a tank that reads 1.2 would be held on a flag."""
    saved(hass, {"temperature": 60, "stale_temperature": True})
    with freeze_time(T0):
        await heating(hass, temperature="1.2")
        assert stale(hass) is None
        assert relay(hass) == STATE_ON


async def test_a_count_goes_on_over_a_restart_if_the_reading_has_not_moved(
    hass, world  # noqa: F811
):
    """Or a restart every 25 minutes of heating would keep a dead sensor alive."""
    saved(hass, {"temperature": 60}, watch_value=40.0, watch_heated_s=25 * 60)
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await heating(hass)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 4)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF, "25 minutes before the restart and 5 after"
        assert stale(hass) == 40


async def test_a_restored_count_is_dropped_by_a_reading_that_moved(hass, world):  # noqa: F811
    saved(hass, {"temperature": 60}, watch_value=40.0, watch_heated_s=25 * 60)
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await heating(hass, temperature="41")
        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 1)
        assert relay(hass) == STATE_OFF


async def test_a_count_learned_on_one_sensor_is_not_restored_for_another(hass, world):  # noqa: F811
    saved(
        hass,
        {"temperature": 60},
        sensor="sensor.the_one_that_died",
        watch_value=40.0,
        watch_heated_s=25 * 60,
    )
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await heating(hass)
        await tick(hass, frozen, 29)
        assert relay(hass) == STATE_ON
        assert stale(hass) is None


async def test_a_restored_count_never_switches_an_element_off_by_itself(hass, world):  # noqa: F811
    """A full count restored is still only a count: it needs heat to act on."""
    saved(hass, {"temperature": 60}, mode=STATE_OFF, watch_value=40.0, watch_heated_s=30 * 60)
    with freeze_time(T0) as frozen:
        entry = await heating(hass)
        assert heater(hass).state == STATE_OFF
        await tick(hass, frozen, 120)
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None


async def test_the_stored_copy_carries_the_count_and_its_sensor(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await heating(hass)
        await tick(hass, frozen, 10)
        stored = entities(hass)["Upstairs"].extra_restore_state_data
        data = json.loads(json.dumps(stored.as_dict(), cls=JSONEncoder))
        assert data["reading_sensor"] == UPSTAIRS_SENSOR
        assert data["watch_value"] == 40
        assert data["watch_heated_s"] == 600
        back = HeaterStoredData.from_dict(data)
        assert (back.reading_sensor, back.watch_value, back.watch_heated_s) == (
            UPSTAIRS_SENSOR,
            40,
            600,
        )
        # The count is not published: it moves with the clock.
        assert "watch_heated_s" not in heater(hass).attributes


def test_a_stored_copy_from_before_this_version_reads_as_no_count():
    back = HeaterStoredData.from_dict({"state": "electric", "attributes": {}})
    assert (back.reading_sensor, back.watch_value, back.watch_heated_s) == (None, None, 0.0)


def test_junk_in_the_stored_copy_reads_as_no_count():
    back = HeaterStoredData.from_dict(
        {
            "state": "electric",
            "attributes": {},
            "reading_sensor": 7,
            "watch_value": True,
            "watch_heated_s": "soon",
        }
    )
    assert (back.reading_sensor, back.watch_value, back.watch_heated_s) == (None, None, 0.0)


# --- housekeeping -------------------------------------------------------------------


async def test_diagnostics_show_the_count(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        entry = await heating(hass)
        await tick(hass, frozen, 10)
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["stale_temperature"] is None
        assert report["reading_watch_baseline"] == 40
        assert report["heated_now"] is True
        assert report["heated_without_a_move_s"] == 600
        assert report["timers_armed"]["_stale_reading_timer"] is True

        await tick(hass, frozen, 20)
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["stale_temperature"] == 40
        assert report["heated_now"] is False
        assert report["timers_armed"]["_stale_reading_timer"] is False


async def test_unloading_stops_the_count(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        entry = await heating(hass)
        await tick(hass, frozen, 10)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await tick(hass, frozen, 60)
        assert issue(hass, STALE, entry) is None


async def test_removing_the_entry_removes_its_repair(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        assert issue(hass, STALE, entry) is not None
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert issue(hass, STALE, entry) is None
