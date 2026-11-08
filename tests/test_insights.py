"""4.6.3 insights: heat-up rate, Ready At, energy by source, forecast pre-heat."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater.config_flow import _forecast_preheat_errors
from custom_components.generic_water_heater.energy_split import (
    MAX_GAP,
    EnergySplit,
    source_for,
)
from custom_components.generic_water_heater.heat_rate import (
    HeatRateLearner,
    ready_at,
)
from tests.test_eco_inputs import (  # noqa: F401
    BOOST,
    heater,
    set_state,
    setup_inputs,
    switch_on,
)
from tests.test_element_power import METER, _meter, _setup
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    world,
)

RATE = "sensor.upstairs_heat_up_rate"
READY = "sensor.upstairs_ready_at"
SURPLUS_KWH = "sensor.upstairs_energy_today_surplus"
UTILITY_KWH = "sensor.upstairs_energy_today_utility"
OTHER_KWH = "sensor.upstairs_energy_today_other"
FORECAST = "sensor.energy_production_tomorrow"
T = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
BEIRUT = ZoneInfo("Asia/Beirut")


# --- heat-up rate, pure -------------------------------------------------------


def test_a_run_teaches_the_rate():
    learner = HeatRateLearner()
    learner.element(True, T, 40.0)
    learner.reading(T + timedelta(minutes=20), 43.0)
    assert learner.element(False, T + timedelta(minutes=21), 43.0)
    assert learner.rate == pytest.approx(9.0)
    assert learner.runs == 1


@pytest.mark.parametrize(
    ("minutes", "rise"),
    [(5, 3.0), (30, 0.5)],  # too short to measure; too small a rise
)
def test_runs_too_small_to_measure_are_ignored(minutes, rise):
    learner = HeatRateLearner()
    learner.element(True, T, 40.0)
    learner.reading(T + timedelta(minutes=minutes), 40.0 + rise)
    assert not learner.element(False, T + timedelta(minutes=minutes), 40.0 + rise)
    assert learner.rate is None


def test_one_odd_run_moves_the_average_only_partly():
    learner = HeatRateLearner(rate=9.0, runs=5)
    learner.element(True, T, 40.0)
    learner.reading(T + timedelta(hours=1), 43.0)  # a shower mid-run: 3 C/h
    learner.element(False, T + timedelta(hours=1), 43.0)
    assert learner.rate == pytest.approx(0.3 * 3.0 + 0.7 * 9.0)


def test_ready_at():
    assert ready_at(T, 45.0, 53.0, 8.0, True) == T + timedelta(hours=1)
    assert ready_at(T, 45.0, 53.0, 8.0, False) is None  # not heating
    assert ready_at(T, 53.0, 53.0, 8.0, True) is None  # already there
    assert ready_at(T, 45.0, 53.0, None, True) is None  # nothing learned yet


# --- energy split, pure -------------------------------------------------------


def test_each_interval_goes_to_the_source_at_its_start():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=BEIRUT)
    split = EnergySplit(now.date())
    split.advance(now, 2000.0, "surplus")
    split.advance(now + timedelta(minutes=6), 2000.0, "utility")
    split.advance(now + timedelta(minutes=9), 0.0, "utility")
    assert split.kwh["surplus"] == pytest.approx(0.2)
    assert split.kwh["utility"] == pytest.approx(0.1)
    assert split.kwh["other"] == 0


def test_an_unobserved_gap_is_not_credited():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=BEIRUT)
    split = EnergySplit(now.date())
    split.advance(now, 2000.0, "surplus")
    split.advance(now + MAX_GAP + timedelta(seconds=1), 2000.0, "surplus")
    assert split.kwh["surplus"] == 0


def test_midnight_splits_the_interval_and_resets():
    before = datetime(2026, 11, 4, 23, 57, tzinfo=BEIRUT)
    split = EnergySplit(before.date())
    split.kwh["other"] = 5.0
    split.advance(before, 1200.0, "other")
    split.advance(before + timedelta(minutes=6), 1200.0, "other")
    assert split.day == date(2026, 11, 5)
    assert split.kwh["other"] == pytest.approx(0.06)  # 3 minutes after midnight


def test_a_restored_value_counts_only_on_its_own_day():
    split = EnergySplit(date(2026, 11, 5))
    split.restore("surplus", date(2026, 11, 4), 3.0)
    split.restore("utility", date(2026, 11, 5), 1.5)
    assert split.kwh == {"surplus": 0.0, "utility": 1.5, "other": 0.0}


def test_sources():
    assert source_for(True, True) == "surplus"
    assert source_for(False, True) == "utility"
    assert source_for(False, False) == "other"


# --- on a running heater --------------------------------------------------------


async def tick(hass, frozen, minutes):
    frozen.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def reading(hass, value):
    hass.states.async_set(UPSTAIRS_SENSOR, str(value), {"device_class": "temperature"})
    await hass.async_block_till_done()


async def test_the_rate_is_learned_from_a_metered_run_and_feeds_ready_at(hass, world):
    with freeze_time(TRIGGER) as frozen:
        _meter(hass, "0")
        await _setup(hass, power_sensor=METER, target_temp=60.0)
        assert hass.states.get(RATE).state == "unknown"

        _meter(hass, "1500")
        await hass.async_block_till_done()
        for step in range(1, 7):  # 40 -> 43 C over 20 minutes: 9 C/h
            await tick(hass, frozen, 20 / 6)
            await reading(hass, 40 + step * 0.5)
        _meter(hass, "0")
        await hass.async_block_till_done()
        assert float(hass.states.get(RATE).state) == pytest.approx(9.0, rel=0.02)
        assert hass.states.get(READY).state == "unknown", "not drawing: no estimate"

        _meter(hass, "1500")
        await hass.async_block_till_done()
        ready = dt_util.parse_datetime(hass.states.get(READY).state)
        # 43 -> 60 C at 9 C/h is about 1 h 53 min away.
        assert (ready - dt_util.utcnow()).total_seconds() == pytest.approx(
            17 / 9 * 3600, abs=180
        )
        assert hass.states.get(READY).attributes["heading_for"] == 60.0


async def test_energy_today_by_source(hass, world):
    with freeze_time(TRIGGER) as frozen:
        _meter(hass, "2000")
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await _setup(hass, power_sensor=METER, surplus_entity=PV_EXCESS, surplus_state="on")
        for _ in range(30):  # a steady draw reports nothing; the flush credits it
            await tick(hass, frozen, 1)
        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        for _ in range(15):
            await tick(hass, frozen, 1)
        assert float(hass.states.get(SURPLUS_KWH).state) == pytest.approx(1.0, abs=0.01)
        assert float(hass.states.get(OTHER_KWH).state) == pytest.approx(0.5, abs=0.01)
        assert float(hass.states.get(UTILITY_KWH).state) == 0
        assert hass.states.get(SURPLUS_KWH).attributes["state_class"] == "total"


async def test_switching_an_insight_off_removes_its_entities(hass, world):
    _meter(hass, "0")
    entry = await _setup(hass, power_sensor=METER)
    registry = er.async_get(hass)
    for entity_id in (RATE, READY, SURPLUS_KWH, "binary_sensor.upstairs_element_fault"):
        assert registry.async_get(entity_id) is not None, entity_id

    hass.config_entries.async_update_entry(
        entry,
        options={
            "enable_heat_up_rate": False,
            "enable_energy_split": False,
            "enable_element_fault": False,
        },
    )
    await hass.async_block_till_done()

    for entity_id in (RATE, READY, SURPLUS_KWH, "binary_sensor.upstairs_element_fault"):
        assert registry.async_get(entity_id) is None, entity_id
        assert hass.states.get(entity_id) is None, entity_id


# --- forecast pre-heat -----------------------------------------------------------


async def preheating(hass, forecast_kwh, **kwargs):
    """Normal target 50, no boost of its own, tank at 52, surplus on."""
    hass.states.async_set(FORECAST, str(forecast_kwh), {"unit_of_measurement": "kWh"})
    await setup_inputs(
        hass,
        temperature="52",
        enable_forecast_preheat=True,
        forecast_entity=FORECAST,
        forecast_min_kwh=20.0,
        forecast_preheat_target_temperature=61.0,
        **kwargs,
    )
    await hass.services.async_call("switch", "turn_on", {"entity_id": BOOST}, blocking=True)
    await hass.async_block_till_done()
    await set_state(hass, PV_EXCESS, STATE_ON)


async def test_a_poor_forecast_stores_heat_on_surplus(hass, world):
    await preheating(hass, 12.5)
    assert switch_on(hass)
    assert heater(hass).attributes["forecast_preheat_active"] is True
    assert heater(hass).attributes["surplus_boost_active"] is True


async def test_a_good_forecast_changes_nothing(hass, world):
    await preheating(hass, 35.1)
    assert not switch_on(hass)
    assert heater(hass).attributes["forecast_preheat_active"] is False


async def test_the_forecast_turning_poor_starts_it(hass, world):
    await preheating(hass, 35.1)
    assert not switch_on(hass)
    await set_state(hass, FORECAST, "9000")  # Wh this time: 9 kWh
    hass.states.async_set(FORECAST, "9000", {"unit_of_measurement": "Wh"})
    await hass.async_block_till_done()
    assert switch_on(hass)


async def test_the_boost_switch_turns_pre_heat_off_too(hass, world):
    await preheating(hass, 12.5)
    await hass.services.async_call("switch", "turn_off", {"entity_id": BOOST}, blocking=True)
    await hass.async_block_till_done()
    assert not switch_on(hass)


async def test_an_unreadable_forecast_does_not_pre_heat(hass, world):
    await preheating(hass, "unavailable")
    assert not switch_on(hass)


def test_pre_heat_needs_its_settings():
    base = {"enable_forecast_preheat": True, "surplus_entity": PV_EXCESS}
    assert _forecast_preheat_errors({"enable_forecast_preheat": False}) == {}
    assert _forecast_preheat_errors(base) == {"base": "forecast_preheat_incomplete"}
    assert _forecast_preheat_errors(
        {**base, "surplus_entity": ""}
    ) == {"base": "surplus_boost_needs_surplus_entity"}
    assert _forecast_preheat_errors(
        {
            **base,
            "forecast_entity": FORECAST,
            "forecast_min_kwh": 20,
            "forecast_preheat_target_temperature": 61,
        }
    ) == {}


# --- across a restart --------------------------------------------------------------


async def test_the_rate_and_todays_energy_survive_a_restart(hass, world):
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import (
        mock_restore_cache_with_extra_data,
    )

    with freeze_time(TRIGGER):
        today = dt_util.start_of_local_day()
        mock_restore_cache_with_extra_data(
            hass,
            (
                (
                    State(RATE, "8.5", {"runs_learned": 4}),
                    {"native_value": 8.5, "native_unit_of_measurement": "°C/h"},
                ),
                (
                    State(SURPLUS_KWH, "1.25", {"last_reset": today.isoformat()}),
                    {"native_value": 1.25, "native_unit_of_measurement": "kWh"},
                ),
                (
                    State(
                        UTILITY_KWH,
                        "2.0",
                        {"last_reset": (today - timedelta(days=1)).isoformat()},
                    ),
                    {"native_value": 2.0, "native_unit_of_measurement": "kWh"},
                ),
            ),
        )
        _meter(hass, "0")
        await _setup(hass, power_sensor=METER)

        assert float(hass.states.get(RATE).state) == 8.5
        assert hass.states.get(RATE).attributes["runs_learned"] == 4
        assert float(hass.states.get(SURPLUS_KWH).state) == 1.25
        assert float(hass.states.get(UTILITY_KWH).state) == 0, "yesterday's total"
