"""Guards of the stale-reading failsafe that nothing pinned at first.

Each test names a mutation that left the rest of the suite green: a reviewer
removed the guard on a copy, the whole suite passed, and the test below is
what went red. The behaviour itself is in test_stale_reading.py.
"""

import logging
from datetime import timedelta

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

from custom_components.generic_water_heater.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.generic_water_heater.water_heater import HeaterStoredData
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    entities,
    world,
)
from tests.test_stale_reading import (
    HELD,
    STALE,
    T0,
    UPSTAIRS,
    heater,
    heating,
    held,
    issue,
    relay,
    saved,
    stale,
    tank,
    tick,
)


async def test_a_held_reading_stops_the_count(hass, world):  # noqa: F811
    """Mutant: _hold_stale_reading does not tell the watch heating has stopped.

    The watch went on counting heated time through the hold, so a retry that
    leaves the element parked would later trip on time it never ran, and the
    stored copy carried a count that kept growing.
    """
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        await tick(hass, frozen, 10)
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["heated_now"] is False
        assert report["heated_without_a_move_s"] == 1800


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("set_operation_mode", {"operation_mode": STATE_ELECTRIC}),
        ("turn_on", {}),
    ],
)
async def test_asking_for_heat_is_not_a_fresh_count_when_nothing_is_held(
    hass, world, service, data  # noqa: F811
):
    """Mutant: _retry_stale_reading without its `nothing is held` guard.

    Every turn_on or mode choice on a healthy heater restarted the count, so a
    dead sensor on a tank whose owner touches the card every 20 minutes was
    never found.
    """
    with freeze_time(T0) as frozen:
        entry = await heating(hass)
        await tick(hass, frozen, 20)
        await hass.services.async_call(
            "water_heater", service, {"entity_id": UPSTAIRS, **data}, blocking=True
        )
        await hass.async_block_till_done()
        await tick(hass, frozen, 10)
        assert stale(hass) == 40, "30 minutes of heat on one number, the count was not reset"
        assert relay(hass) == STATE_OFF
        assert issue(hass, STALE, entry) is not None


async def test_asking_for_heat_before_the_sensor_has_spoken_starts_no_clocks(
    hass, world  # noqa: F811
):
    """Mutant: _retry_stale_reading without its `awaiting the first reading` branch.

    The sensor has said nothing since the restart, which is "not yet", not a
    lost reading. Without the branch the lost-reading clocks start at once and
    the "sensor unavailable" repair arrives ten minutes after the person's
    request instead of thirteen minutes after the load (the wait is three).
    """
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        entry = await heating(hass, temperature=STATE_UNAVAILABLE)
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await hass.async_block_till_done()
        await tick(hass, frozen, 2.5)
        assert entities(hass)["Upstairs"]._failsafe_warned is False
        await tick(hass, frozen, 8.5)  # 11 minutes in
        assert issue(hass, "temperature_sensor_unavailable", entry) is None


async def test_asking_for_heat_while_the_sensor_says_nothing_starts_the_warning(
    hass, world, caplog  # noqa: F811
):
    """Mutant: _retry_stale_reading does not start the failsafe warning.

    A hold has `_temperature_lost` set, so the sensor's own unavailable report
    started no clocks. The retry is the one place that starts them.
    """
    with freeze_time(T0) as frozen:
        await held(hass, frozen)
        tank(hass, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await hass.async_block_till_done()
        with caplog.at_level(logging.WARNING):
            await tick(hass, frozen, 3)
        assert entities(hass)["Upstairs"]._failsafe_warned is True
        assert any("holding heater" in r.getMessage() for r in caplog.records)


async def test_a_number_exactly_a_band_from_the_stuck_one_ends_the_hold(hass, world):  # noqa: F811
    """Mutant: `< STALE_READING_BAND` -> `<=` in _async_sensor_changed.

    The count moves at exactly the band (test_a_reading_that_moves_is_a_live_sensor),
    so a hold must end at exactly the band too; otherwise the two disagree.
    """
    with freeze_time(T0) as frozen:
        entry = await held(hass, frozen)
        tank(hass, "40.5")
        await hass.async_block_till_done()
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert relay(hass) == STATE_ON


async def test_a_restart_onto_a_number_exactly_a_band_away_ends_the_hold(hass, world):  # noqa: F811
    """Mutant: `< STALE_READING_BAND` -> `<=` in _take_startup_reading."""
    saved(hass, HELD)
    with freeze_time(T0):
        entry = await heating(hass, temperature="40.5")
        assert stale(hass) is None
        assert issue(hass, STALE, entry) is None
        assert relay(hass) == STATE_ON


async def test_the_same_number_back_after_a_lost_reading_stops_the_lost_clocks(
    hass, world  # noqa: F811
):
    """Mutant: the `same number again` branch leaves the lost-reading clocks running.

    A restored hold whose sensor stayed silent ran out the first-reading wait
    and started the ordinary lost-reading clocks. The sensor then speaks, with
    the old number: it is back, and what is wrong with it has its own repair.
    """
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        entry = await heating(hass, temperature=STATE_UNAVAILABLE)
        await tick(hass, frozen, 3)  # the wait ends, the clocks start
        await tick(hass, frozen, 3)
        tank(hass, "40")
        await hass.async_block_till_done()
        assert stale(hass) == 40
        await tick(hass, frozen, 20)
        assert issue(hass, "temperature_sensor_unavailable", entry) is None
        assert issue(hass, STALE, entry) is not None


async def test_the_old_number_after_a_silent_restart_switches_an_element_that_is_on_off(
    hass, world  # noqa: F811
):
    """Mutant: the `same number again` branch runs no control pass.

    A restored hold is pending while the sensor is silent and commands nothing.
    The relay is found on. The first number comes and it is the old one: the
    hold goes on, and the element has to go off now, not at some later event.
    """
    saved(hass, HELD)
    with freeze_time(T0):
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await heating(hass, temperature=STATE_UNAVAILABLE)
        assert world["turn_off"] == [] and relay(hass) == STATE_ON, "premise: pending"
        tank(hass, "40")
        await hass.async_block_till_done()
        assert stale(hass) == 40
        assert relay(hass) == STATE_OFF
        assert world["turn_off"] != []


async def test_a_restored_count_is_not_taken_up_for_another_sensor(hass, world):  # noqa: F811
    """Mutant: the restored count no longer checks which sensor it was counted on.

    `reading_sensor` names the sensor the count belongs to; what was counted on
    one sensor is not held against another configured in its place.
    """
    saved(
        hass,
        {"temperature": 60},
        sensor="sensor.the_old_tank_sensor",
        watch_value=40.0,
        watch_heated_s=25 * 60,
    )
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        await heating(hass)
        assert relay(hass) == STATE_ON
        await tick(hass, frozen, 6)
        assert relay(hass) == STATE_ON, "25 minutes of another sensor's count were used"
        assert stale(hass) is None


async def test_a_confirmed_restored_hold_arms_no_first_reading_wait(hass, world):  # noqa: F811
    """Mutant: the startup wait is armed although the startup read confirmed the hold.

    The number was read and it is the stuck one; the wait is for a sensor that
    has not spoken. Armed anyway, its end called an available sensor a lost
    one and the "sensor unavailable" repair appeared against a sensor that
    reads a number.
    """
    saved(hass, HELD)
    with freeze_time(T0) as frozen:
        entry = await heating(hass)  # reads 40 at the load
        assert stale(hass) == 40
        assert entities(hass)["Upstairs"]._awaiting_first_reading is False
        await tick(hass, frozen, 20)
        assert issue(hass, "temperature_sensor_unavailable", entry) is None
        assert entities(hass)["Upstairs"]._failsafe_warned is False


def test_a_stored_count_that_is_not_finite_reads_as_none():
    """Mutant: `_number` returns float(value) instead of a finite number."""
    back = HeaterStoredData.from_dict(
        {
            "state": "electric",
            "attributes": {},
            "reading_sensor": "sensor.x",
            "watch_value": float("nan"),
            "watch_heated_s": float("inf"),
        }
    )
    assert back.watch_value is None
    assert back.watch_heated_s == 0.0
