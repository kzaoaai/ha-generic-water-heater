"""Tests for the Legionella thermal-conditions sensor.

The model reports thermal conditions at one sensor. The behaviour that matters
most is what it REFUSES to credit: a hold that was interrupted, a spike between
two widely spaced samples, and time nobody observed.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.generic_water_heater import (
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_LEGIONELLA_INTERVAL_DAYS,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    auto_enable_custom_integrations,
    world,
)
from custom_components.generic_water_heater.sensor import (
    DISINFECTION_HOLD,
    DISINFECTION_TEMP_C,
    MAX_CREDITED_GAP,
    STATE_ELEVATED,
    STATE_HIGH,
    STATE_LOW,
    STATE_UNKNOWN_RISK,
    LegionellaRiskSensor,
)

BASE = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    """Return a timestamp ``minutes`` after the base."""
    return BASE + timedelta(minutes=minutes)


def build(interval_days: int = 7) -> LegionellaRiskSensor:
    """Return a sensor not attached to hass, driven directly."""
    return LegionellaRiskSensor(
        name="Upstairs",
        source_sensor_entity_id="sensor.upstairs_temperature",
        device_identifier="01JQ0000000000000000UPSTRS",
        device_identifiers=None,
        interval_days=interval_days,
    )


def feed(sensor, samples):
    """Feed (minutes, temperature) samples in order."""
    for minutes, temperature in samples:
        sensor._async_add_sample(temperature, at(minutes))


def steady(start_min, end_min, temperature, step=5):
    """Return samples holding a temperature across a span."""
    return [(m, temperature) for m in range(start_min, end_min + 1, step)]


# ---------------------------------------------------------------------------
# Completing a cycle
# ---------------------------------------------------------------------------


def test_a_full_hour_at_60_completes_a_cycle():
    """The one thing that should count: 60 C held for a continuous hour."""
    sensor = build()
    feed(sensor, steady(0, 65, 60.5))

    assert sensor._last_disinfection_at is not None
    assert sensor._last_disinfection_at >= at(60)


def test_a_hold_just_short_of_an_hour_does_not_count():
    """59 minutes is not an hour; nothing is banked."""
    sensor = build()
    feed(sensor, steady(0, 55, 60.5))

    assert sensor._last_disinfection_at is None
    assert sensor._hold_seconds == pytest.approx(55 * 60)


def test_an_interrupted_hold_is_discarded_not_banked():
    """Partial treatment is the failure mode, not partial credit."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))
    assert sensor._hold_seconds > 0

    # Element cuts out, tank slips below the threshold, then recovers.
    feed(sensor, [(55, 58.0)])
    assert sensor._hold_seconds == 0.0, "a partial hold survived an interruption"

    feed(sensor, steady(60, 100, 60.5))
    assert sensor._last_disinfection_at is None, "two partial holds were added together"


def test_a_hold_resumed_after_a_dip_must_start_over():
    """The full hour has to be continuous."""
    sensor = build()
    feed(sensor, steady(0, 50, 61.0))
    feed(sensor, [(52, 55.0)])
    feed(sensor, steady(55, 120, 61.0))

    assert sensor._last_disinfection_at is not None
    # Completion must come from the second run, not the sum of both.
    assert sensor._last_disinfection_at >= at(115)


def test_unobserved_gaps_are_not_credited_as_held_temperature():
    """A restart is missing evidence, not proof the tank stayed hot."""
    sensor = build()
    gap = MAX_CREDITED_GAP.total_seconds() / 60 + 10

    feed(sensor, [(0, 61.0), (gap, 61.0), (gap + 5, 61.0)])

    # Only the final 5-minute step is credited.
    assert sensor._hold_seconds == pytest.approx(5 * 60)
    assert sensor._last_disinfection_at is None


def test_below_threshold_never_counts_however_long():
    """59 C for a day is not a cycle -- this is the whole safety point."""
    sensor = build()
    feed(sensor, [(m, 59.0) for m in range(0, 1441, 10)])

    assert sensor._last_disinfection_at is None
    assert sensor._hold_seconds == 0.0


# ---------------------------------------------------------------------------
# Risk banding
# ---------------------------------------------------------------------------


def test_unknown_until_there_is_enough_history_to_mean_anything():
    """Silence is not evidence until we have watched for a full interval."""
    sensor = build(interval_days=7)
    with freeze_time(at(120)):
        feed(sensor, [(0, 42.0), (60, 43.0)])
        sensor._recalculate()

    assert sensor.native_value == STATE_UNKNOWN_RISK


def test_never_disinfected_escalates_once_we_have_watched_long_enough():
    """After two intervals of never reaching temperature, say so plainly."""
    sensor = build(interval_days=1)
    feed(sensor, [(0, 42.0)])

    with freeze_time(at(60 * 30)):  # 1.25 intervals of history
        sensor._recalculate()
        assert sensor.native_value == STATE_ELEVATED

    with freeze_time(at(60 * 24 * 3)):  # 3 intervals
        sensor._recalculate()
        assert sensor.native_value == STATE_HIGH


def test_a_completed_cycle_reads_low():
    sensor = build()
    with freeze_time(at(60 * 24)):
        sensor._last_disinfection_at = at(0)
        sensor._recalculate()

    assert sensor.native_value == STATE_LOW


def test_a_cycle_ages_out_to_elevated_then_high():
    sensor = build(interval_days=7)
    sensor._last_disinfection_at = at(0)

    with freeze_time(at(60 * 24 * 10)):
        sensor._recalculate()
        assert sensor.native_value == STATE_ELEVATED

    with freeze_time(at(60 * 24 * 20)):
        sensor._recalculate()
        assert sensor.native_value == STATE_HIGH


# ---------------------------------------------------------------------------
# Window metrics
# ---------------------------------------------------------------------------


def test_time_at_a_growth_temperature_is_counted():
    """A tank sitting at 42 C is in the growth band for every hour of it."""
    sensor = build()
    feed(sensor, steady(0, 600, 42.0))

    _, growth_hours, lethality, peak = sensor._window_metrics()

    assert growth_hours == pytest.approx(10.0, abs=0.05)
    assert lethality == 0.0, "no kill credit below the validated band"
    assert peak == 42.0


def test_the_47c_plateau_counts_as_growth_not_as_progress():
    """Both real tanks top out at ~48 C. That must not read as safe.

    The HSE risk trigger stops at 45 C, but measured multiplication continues to
    48.4-50.0 C, so a tank plateauing at 47 C is still growing and is earning no
    disinfection credit either -- the worst of both worlds.
    """
    sensor = build()
    feed(sensor, steady(0, 600, 47.0))

    _, growth_hours, lethality, _ = sensor._window_metrics()

    assert growth_hours == pytest.approx(10.0, abs=0.05), "47 C reported as out of band"
    assert lethality == 0.0, "sub-50 C was credited as disinfection"


def test_a_spike_between_distant_samples_cannot_claim_the_gap():
    """Intervals are credited at the lower endpoint, never the peak."""
    sensor = build()
    feed(sensor, [(0, 40.0), (25, 62.0), (30, 40.0)])

    _, _, lethality, peak = sensor._window_metrics()

    assert peak == 62.0
    assert lethality == 0.0, "a spike claimed credit for the whole interval"


def test_kill_credit_accrues_only_inside_the_validated_band():
    """The kinetics are published for 51-61 C; above that is extrapolation."""
    sensor = build()
    feed(sensor, steady(0, 60, 61.0))
    _, _, at_61, _ = sensor._window_metrics()

    hotter = build()
    feed(hotter, steady(0, 60, 75.0))
    _, _, at_75, _ = hotter._window_metrics()

    assert at_61 > 0
    assert at_75 == pytest.approx(at_61), "credit was extrapolated above 61 C"


def test_unavailable_samples_are_ignored():
    sensor = build()
    feed(sensor, [(0, 61.0)])
    sensor._async_add_sample(STATE_UNAVAILABLE, at(5))
    sensor._async_add_sample("not-a-number", at(10))

    assert len(sensor._history) == 1


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_sensor_is_created_only_when_enabled(hass, world):  # noqa: F811
    """Opt-in, like the 7-day maximum sensor."""
    from tests.test_integration_setup import build_entry

    entry = build_entry("Upstairs", "switch.upstairs_element", "sensor.upstairs_water_temperature", 2000.0)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.upstairs_legionella_risk") is None

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.data,
            CONF_ENABLE_LEGIONELLA_SENSOR: True,
            CONF_LEGIONELLA_INTERVAL_DAYS: 7,
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.upstairs_legionella_risk")
    assert state is not None
    assert state.attributes["disinfection_temperature_c"] == DISINFECTION_TEMP_C
    assert state.attributes["disinfection_interval_days"] == 7
    assert "caveat" in state.attributes


# ---------------------------------------------------------------------------
# Regression: the real 200 L tank's thermostat ripple, 2026-08-27
# ---------------------------------------------------------------------------


def test_thermostat_ripple_does_not_abandon_the_hold():
    """Replays the real upstairs tank, which a strict threshold would fail.

    The mechanical thermostat cycles: slow decay below 60 C, then fast re-heat.
    Observed dips lasted 19 and 28 minutes and bottomed at 59.6 C, while the
    tank sat at pasteurisation temperature for five hours. A rule that reported
    that tank as never disinfected would be wrong, and it is exactly what a bare
    "continuous hour at or above 60 C" produces.
    """
    sensor = build()
    samples = []
    samples += [(m, 60.9 - 0.025 * m) for m in range(0, 36, 3)]      # 60.9 -> 60.0
    samples += [(m, 59.6 + 0.02 * (m - 38)) for m in range(38, 58, 3)]  # dip to 59.6
    samples += [(m, 60.0 + 0.015 * (m - 58)) for m in range(58, 82, 3)]  # back above 60
    samples += [(m, 59.7) for m in range(84, 112, 3)]                 # second, longer dip
    samples += [(m, 60.0 + 0.05 * (m - 112)) for m in range(112, 190, 3)]  # re-heat

    feed(sensor, samples)

    assert sensor._hold_open, "thermostat ripple abandoned the hold"
    assert sensor._last_disinfection_at is not None, (
        "five hours at pasteurisation temperature failed to register a cycle"
    )


def test_ripple_time_below_60_is_not_credited_toward_the_hour():
    """Ripple keeps the window open; it does not earn time at temperature."""
    sensor = build()
    # 30 min at 60.5, then 30 min at 59.5 (in the band, below the threshold).
    feed(sensor, steady(0, 30, 60.5))
    at_30 = sensor._hold_seconds
    feed(sensor, [(m, 59.5) for m in range(33, 61, 3)])

    assert sensor._hold_open, "a shallow dip closed the window"
    assert sensor._hold_seconds == pytest.approx(at_30), (
        "time below the disinfection temperature was credited"
    )
    assert sensor._last_disinfection_at is None


def test_sitting_just_below_the_threshold_never_opens_a_hold():
    """59.5 C forever is not a cycle, however long it lasts."""
    sensor = build()
    feed(sensor, [(m, 59.5) for m in range(0, 601, 5)])

    assert not sensor._hold_open
    assert sensor._last_disinfection_at is None


def test_a_deep_drop_still_abandons_the_hold():
    """Hysteresis is for ripple, not for a session that actually stopped."""
    sensor = build()
    feed(sensor, steady(0, 45, 61.0))
    feed(sensor, [(50, 56.0)])

    assert not sensor._hold_open
    assert sensor._hold_seconds == 0.0
