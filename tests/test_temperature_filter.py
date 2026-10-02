"""Tests for the single-sample temperature spike filter.

The regression is the 2026-10-01 trace: a tank at 61.9 C, 13.5 minutes into a
disinfection hold, read 32.9 C once, 1.7 s after its relay switched, then 62.0 C
a second later. The legionella sensor discarded the hold on that one reading.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_UNAVAILABLE
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater import (
    CONF_ENABLE_HOT_WATER_IN_USE,
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
)
from custom_components.generic_water_heater.temperature_filter import (
    SPIKE_CONFIRM,
    SPIKE_JUMP_C,
    Reading,
    SpikeFilter,
)
from custom_components.generic_water_heater.temperature_tracking import (
    async_track_filtered_temperature,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

BASE = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def values(readings):
    return [r.value for r in readings]


# ---------------------------------------------------------------------------
# The pure filter
# ---------------------------------------------------------------------------


def test_small_steps_pass_straight_through():
    f = SpikeFilter()
    assert values(f.add(at(0), 61.9)) == [61.9]
    assert values(f.add(at(10), 61.9 - SPIKE_JUMP_C)) == [61.9 - SPIKE_JUMP_C]


def test_the_first_reading_is_never_questioned():
    assert values(SpikeFilter().add(at(0), 32.9)) == [32.9]


def test_a_spike_that_recovers_is_dropped():
    f = SpikeFilter()
    f.add(at(0), 61.9)
    assert f.add(at(54), 32.9) == []
    assert values(f.add(at(55), 62.0)) == [62.0]
    assert f.pending is None


def test_an_upward_spike_is_dropped_too():
    f = SpikeFilter()
    f.add(at(0), 45.0)
    assert f.add(at(1), 85.0) == []
    assert values(f.add(at(2), 45.1)) == [45.1]


def test_a_step_that_persists_is_delivered_with_its_own_timestamp():
    f = SpikeFilter()
    f.add(at(0), 61.9)
    f.add(at(10), 40.0)
    assert f.add(at(20), 40.2) == [Reading(at(10), 40.0), Reading(at(20), 40.2)]


def test_a_second_jump_from_a_confirmed_step_is_judged_against_it():
    f = SpikeFilter()
    f.add(at(0), 20.0)
    f.add(at(1), 40.0)  # pending
    # 60 is far from both 20 and 40: 40 is confirmed as real (it persisted in
    # the sense that we did not come back), 60 is questioned against 40.
    assert values(f.add(at(2), 60.0)) == [40.0]
    assert f.pending == Reading(at(2), 60.0)


def test_expiry_accepts_a_reading_nobody_contradicted():
    f = SpikeFilter()
    f.add(at(0), 61.9)
    f.add(at(10), 40.0)
    assert f.expire() == [Reading(at(10), 40.0)]
    assert values(f.add(at(100), 40.1)) == [40.1]
    assert f.expire() == []


def test_reset_forgets_the_reference():
    f = SpikeFilter()
    f.add(at(0), 61.9)
    f.reset()
    assert values(f.add(at(10), 30.0)) == [30.0]


def test_prime_sets_the_reference_without_delivering():
    f = SpikeFilter()
    f.prime(61.9)
    assert f.add(at(0), 32.9) == []


# ---------------------------------------------------------------------------
# The Home Assistant wrapper
# ---------------------------------------------------------------------------


@pytest.fixture
def received(hass):
    got = []
    unsub = async_track_filtered_temperature(
        hass, UPSTAIRS_SENSOR, lambda value, when, attrs: got.append(value)
    )
    yield got
    unsub()


async def test_wrapper_drops_the_real_trace_spike(hass, world):  # noqa: F811
    hass.states.async_set(UPSTAIRS_SENSOR, "61.9")
    got = []
    unsub = async_track_filtered_temperature(
        hass, UPSTAIRS_SENSOR, lambda value, when, attrs: got.append(value)
    )
    for state in ("32.9", "62.0"):
        hass.states.async_set(UPSTAIRS_SENSOR, state)
        await hass.async_block_till_done()
    unsub()
    assert got == [62.0]


async def test_wrapper_primes_from_the_current_state(hass, world, received):  # noqa: F811
    # The world fixture left the sensor at 40 before the subscription existed.
    hass.states.async_set(UPSTAIRS_SENSOR, "80")
    await hass.async_block_till_done()
    assert received == []


async def test_wrapper_never_delays_unavailable(hass, world, received):  # noqa: F811
    hass.states.async_set(UPSTAIRS_SENSOR, "80")  # pending
    hass.states.async_set(UPSTAIRS_SENSOR, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert received == [STATE_UNAVAILABLE]
    # ...and the gap cleared the reference, so the next reading is not judged.
    hass.states.async_set(UPSTAIRS_SENSOR, "80")
    await hass.async_block_till_done()
    assert received == [STATE_UNAVAILABLE, 80.0]


async def test_wrapper_accepts_a_real_step_after_the_confirm_window(hass, world, received):  # noqa: F811
    hass.states.async_set(UPSTAIRS_SENSOR, "55")
    await hass.async_block_till_done()
    assert received == []
    async_fire_time_changed(hass, dt_util.utcnow() + SPIKE_CONFIRM + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert received == [55.0]


# ---------------------------------------------------------------------------
# End to end: the consumers that acted on the glitch
# ---------------------------------------------------------------------------


async def test_the_control_loop_does_not_heat_on_a_cold_spike(hass, world):  # noqa: F811
    """A satisfied tank must not be switched on by one impossible reading."""
    hass.states.async_set(UPSTAIRS_SENSOR, "61")
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, target_temp=60.0)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": "water_heater.upstairs", "operation_mode": STATE_ELECTRIC},
        blocking=True,
    )
    await hass.async_block_till_done()
    world["turn_on"].clear()
    assert hass.states.get(UPSTAIRS_SWITCH).state == "off"
    assert hass.states.get("water_heater.upstairs").state == STATE_ELECTRIC

    hass.states.async_set(UPSTAIRS_SENSOR, "32.9")
    await hass.async_block_till_done()
    hass.states.async_set(UPSTAIRS_SENSOR, "61.1")
    await hass.async_block_till_done()

    assert world["turn_on"] == []
    assert hass.states.get("water_heater.upstairs").attributes["current_temperature"] == 61.1


async def test_a_disinfection_hold_survives_the_real_trace(hass, world):  # noqa: F811
    """Replays 2026-10-01: 13.5 minutes held, one 32.9 C reading, hold kept."""
    start = datetime(2026, 10, 1, 15, 24, 15, tzinfo=timezone.utc)
    with freeze_time(start) as frozen:
        hass.states.async_set(UPSTAIRS_SENSOR, "59.5")
        entry = build_entry(
            "Upstairs",
            UPSTAIRS_SWITCH,
            UPSTAIRS_SENSOR,
            **{CONF_ENABLE_LEGIONELLA_SENSOR: True},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        for i in range(28):  # 14 minutes at 30 s
            # Distinct consecutive values: an unchanged state fires no event.
            temperature = 60.0 + (i % 20) * 0.1
            hass.states.async_set(UPSTAIRS_SENSOR, f"{temperature:.1f}")
            await hass.async_block_till_done()
            frozen.tick(timedelta(seconds=30))

        before = hass.states.get("sensor.upstairs_legionella_risk").attributes
        assert before["hold_progress_minutes"] >= 13.0

        hass.states.async_set(UPSTAIRS_SENSOR, "32.9")
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=1))
        hass.states.async_set(UPSTAIRS_SENSOR, "62.0")
        await hass.async_block_till_done()

        after = hass.states.get("sensor.upstairs_legionella_risk").attributes
        assert after["hold_in_progress"] is True
        assert after["hold_progress_minutes"] >= before["hold_progress_minutes"]
        assert after["max_temperature_7d"] == 62.0


async def test_the_draw_detector_does_not_fire_on_a_cold_spike(hass, world):  # noqa: F811
    """A 29 C plunge in one reading is the steepest fall it could ever see."""
    start = datetime(2026, 10, 1, 15, 38, 0, tzinfo=timezone.utc)
    with freeze_time(start) as frozen:
        hass.states.async_set(UPSTAIRS_SENSOR, "61.8")
        entry = build_entry(
            "Upstairs",
            UPSTAIRS_SWITCH,
            UPSTAIRS_SENSOR,
            **{CONF_ENABLE_HOT_WATER_IN_USE: True},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        frozen.tick(timedelta(seconds=40))
        hass.states.async_set(UPSTAIRS_SENSOR, "61.9")
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=54))
        hass.states.async_set(UPSTAIRS_SENSOR, "32.9")
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=1))
        hass.states.async_set(UPSTAIRS_SENSOR, "62.0")
        await hass.async_block_till_done()

        assert hass.states.get("binary_sensor.upstairs_hot_water_in_use").state == "off"


async def test_the_seven_day_maximum_does_not_keep_a_hot_spike(hass, world):  # noqa: F811
    hass.states.async_set(UPSTAIRS_SENSOR, "45.0")
    entry = build_entry(
        "Upstairs",
        UPSTAIRS_SWITCH,
        UPSTAIRS_SENSOR,
        **{CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for state in ("85.0", "45.1"):
        hass.states.async_set(UPSTAIRS_SENSOR, state)
        await hass.async_block_till_done()

    maxima = [
        s.state for s in hass.states.async_all("sensor") if "highest" in s.entity_id
    ]
    assert maxima == ["45.1"]
