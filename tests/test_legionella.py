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
    SUSTAIN_HARD_FLOOR_C,
    LegionellaRiskSensor,
    LegionellaStoredData,
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
    """Partial treatment is the failure mode, not partial credit.

    The interruption has to show up in two consecutive samples now -- one
    reading is not enough to throw an hour away -- but a tank that actually
    stopped holding temperature still loses everything.
    """
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))
    assert sensor._hold_seconds > 0

    # Element cuts out, tank slips below the threshold, then recovers.
    feed(sensor, [(55, 58.5), (58, 58.2)])
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


# ---------------------------------------------------------------------------
# Naming and presentation
# ---------------------------------------------------------------------------


def test_the_sensor_carries_the_virus_icon():
    """Requested by design."""
    assert build()._attr_icon == "mdi:virus"


def test_an_unnamed_device_does_not_produce_a_bare_name():
    """Regression: a heater on an unnamed device became "Legionella Risk".

    Some integrations register devices with name=None (localtuya does). Leaving
    Home Assistant to compose "<device> <entity>" then yields a bare, ambiguous
    name -- and with two tanks, two entities claiming to be "Legionella Risk".
    """
    sensor = LegionellaRiskSensor(
        name="Downstairs Water Heater",
        source_sensor_entity_id="sensor.downstairs_temperature",
        device_identifier="01JQ0000000000000000DWNSTR",
        device_identifiers={("some_integration", "device-1")},
        interval_days=7,
        device_has_name=False,
    )

    assert sensor._attr_name == "Downstairs Water Heater Legionella Risk"
    assert sensor._attr_has_entity_name is False


def test_a_named_device_still_supplies_the_prefix():
    """Where the device has a name, let Home Assistant compose as before."""
    sensor = LegionellaRiskSensor(
        name="Upstairs Water Heater",
        source_sensor_entity_id="sensor.upstairs_temperature",
        device_identifier="01JQ0000000000000000UPSTRS",
        device_identifiers={("some_integration", "device-2")},
        interval_days=7,
        device_has_name=True,
    )

    assert sensor._attr_name == "Legionella Risk"
    assert sensor._attr_has_entity_name is True


# ---------------------------------------------------------------------------
# One bad reading must not throw away an hour
# ---------------------------------------------------------------------------


def test_a_single_sample_below_sustain_does_not_discard_the_hold():
    """One implausible reading is not proof the tank left temperature.

    The sensor reads one point on a stratified vessel over a network that has
    dropped out repeatedly on this system. Discarding a nearly complete hold on
    a single sample makes the whole feature hostage to one bad packet.
    """
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))
    banked = sensor._hold_seconds

    feed(sensor, [(53, 58.5)])

    assert sensor._hold_open, "one sample discarded the hold"
    assert sensor._hold_seconds == pytest.approx(banked)


def test_the_forgiven_dip_still_earns_no_credit():
    """Forgiving a dip must not credit the time it spanned."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))
    banked = sensor._hold_seconds

    feed(sensor, [(53, 58.5), (56, 60.5)])

    assert sensor._hold_open
    assert sensor._hold_seconds == pytest.approx(banked), (
        "time spanning a sub-sustain reading was credited toward the hour"
    )


def test_two_consecutive_samples_below_sustain_discard_the_hold():
    """Confirmed by a second reading, the hold goes -- as it always did."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))

    feed(sensor, [(53, 58.5), (56, 58.4)])

    assert not sensor._hold_open
    assert sensor._hold_seconds == 0.0


def test_a_drop_well_below_sustain_discards_immediately():
    """A degree under the sustain floor is a real event, not a bad reading."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))

    feed(sensor, [(53, SUSTAIN_HARD_FLOOR_C - 0.1)])

    assert not sensor._hold_open, "a genuine loss of heat was forgiven"
    assert sensor._hold_seconds == 0.0


def test_a_dip_that_recovers_too_late_discards_the_hold():
    """Recovery outside the sampling cadence is unobserved time, not ripple."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))

    late = 53 + MAX_CREDITED_GAP.total_seconds() / 60 + 5
    feed(sensor, [(53, 58.5), (late, 60.5)])

    assert sensor._hold_seconds == 0.0, "a dip was forgiven across unobserved time"


def test_forgiveness_does_not_leak_into_the_next_hold():
    """A discarded hold starts the next one from zero, grace flag cleared."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))
    feed(sensor, [(53, 58.5), (56, 58.4)])
    assert sensor._sustain_breach_at is None

    feed(sensor, steady(60, 125, 60.5))
    assert sensor._last_disinfection_at is not None
    assert sensor._last_disinfection_at >= at(120), "credit carried over a discard"


# ---------------------------------------------------------------------------
# A hold in flight must survive a restart
# ---------------------------------------------------------------------------


def round_trip(sensor) -> LegionellaStoredData:
    """Return the sensor's restore payload as it comes back from storage."""
    return LegionellaStoredData.from_dict(sensor.extra_restore_state_data.as_dict())


def test_stored_data_carries_the_hold():
    """Without this the payload cannot describe a hold at all."""
    sensor = build()
    feed(sensor, steady(0, 50, 60.5))

    stored = round_trip(sensor)

    assert stored.hold_open is True
    assert stored.hold_seconds == pytest.approx(50 * 60)


def test_a_hold_survives_a_short_restart():
    """Most of a real hold is banked coasting, with the element already off.

    A measured 200 L tank credited its final minutes seven minutes after the
    switch turned off. Dropping that progress on every restart silently threw
    away nearly complete hours.
    """
    live = build()
    feed(live, steady(0, 50, 60.5))
    stored = round_trip(live)

    resumed = build()
    resumed._history = list(live._history)
    resumed._restore_hold(stored, at(52))

    assert resumed._hold_open
    assert resumed._hold_seconds == pytest.approx(50 * 60)

    feed(resumed, steady(55, 75, 60.5))
    assert resumed._last_disinfection_at == at(60), (
        "a resumed hold did not complete on the real elapsed hour"
    )


def test_a_hold_is_not_resumed_after_a_long_outage():
    """Unobserved time is not evidence the tank stayed hot."""
    live = build()
    feed(live, steady(0, 50, 60.5))
    stored = round_trip(live)

    resumed = build()
    resumed._history = list(live._history)
    resumed._restore_hold(stored, at(50) + MAX_CREDITED_GAP + timedelta(minutes=5))

    assert not resumed._hold_open
    assert resumed._hold_seconds == 0.0


def test_a_closed_hold_is_not_resurrected_by_a_restart():
    """Nothing in flight, nothing to restore."""
    live = build()
    feed(live, [(0, 50.0), (5, 50.0)])
    stored = round_trip(live)

    resumed = build()
    resumed._history = list(live._history)
    resumed._restore_hold(stored, at(6))

    assert not resumed._hold_open
    assert resumed._hold_seconds == 0.0


def test_a_restart_cannot_resume_a_hold_the_tank_has_since_lost():
    """Resuming is provisional: the next real sample still gets a veto."""
    live = build()
    feed(live, steady(0, 50, 60.5))
    stored = round_trip(live)

    resumed = build()
    resumed._history = list(live._history)
    resumed._restore_hold(stored, at(52))
    assert resumed._hold_open

    feed(resumed, [(53, 45.0)])
    assert not resumed._hold_open
    assert resumed._hold_seconds == 0.0
