"""Detecting that hot water is being drawn FROM THIS TANK.

A whole-house flow or pump signal cannot say which fixture ran, or whether the
water was hot. What identifies one tank is its own temperature falling faster
than standing loss can manage. These tests replay real measured traces: verified
draws must fire, and the fastest passive cooling ever recorded on these tanks
must not.
"""

from datetime import datetime, timedelta, timezone

import pytest
import homeassistant.util.dt as dt_util

from custom_components.generic_water_heater import binary_sensor as binary_sensor_module
from custom_components.generic_water_heater.binary_sensor import (
    CORROBORATED_RATE_K_PER_MIN,
    DRAW_RATE_K_PER_MIN,
    HotWaterInUseBinarySensor,
)

BASE = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    """Return a timestamp ``seconds`` after the base."""
    return BASE + timedelta(seconds=seconds)


class FakeStates:
    """Minimal states registry so the detector can read its corroborator."""

    def __init__(self, mapping=None):
        self._mapping = dict(mapping or {})

    def get(self, entity_id):
        value = self._mapping.get(entity_id)
        if value is None:
            return None
        if isinstance(value, tuple):
            state, last_updated = value
        else:
            state, last_updated = value, dt_util.utcnow()
        return type("S", (), {"state": state, "last_updated": last_updated})()

    def set(self, entity_id, value):
        self._mapping[entity_id] = value


class FakeHass:
    def __init__(self, mapping=None):
        self.states = FakeStates(mapping)


class FakeClock:
    """Stand in for async_call_later, capturing the pending callback.

    Real, not stubbed: a test can fire the timer and assert the sensor clears
    without any further temperature sample, which is the whole point of having
    the timer at all.
    """

    def __init__(self):
        self.pending = None
        self.scheduled_delays = []

    def __call__(self, hass, delay, callback_fn):
        self.scheduled_delays.append(delay)
        self.pending = callback_fn
        return self._cancel

    def _cancel(self):
        self.pending = None

    def fire(self, sensor):
        """Fire the pending timer, as Home Assistant would."""
        assert self.pending is not None, "no timer was scheduled"
        cb, self.pending = self.pending, None
        cb(None)


def build(pump="on", monkeypatch=None) -> HotWaterInUseBinarySensor:
    """Return a detector not attached to hass, driven directly."""
    sensor = HotWaterInUseBinarySensor(
        name="Upstairs",
        source_sensor_entity_id="sensor.upstairs_temperature",
        water_in_use_entity_id="binary_sensor.house_pump",
        device_identifier="01JQ0000000000000000UPSTRS",
        device_identifiers=None,
    )
    sensor.hass = FakeHass({"binary_sensor.house_pump": pump})
    sensor.async_write_ha_state = lambda: None
    return sensor


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    """Replace async_call_later for every test, and expose it."""
    clock = FakeClock()
    monkeypatch.setattr(binary_sensor_module, "async_call_later", clock)
    return clock


def feed(sensor, samples):
    """Feed (seconds, temperature) samples in order."""
    for seconds, temperature in samples:
        sensor._async_add_sample(temperature, at(seconds))


def ramp(start_c, end_c, seconds, step=20):
    """Return a linear trace from start to end over `seconds`."""
    n = max(1, int(seconds // step))
    return [
        (i * step, start_c + (end_c - start_c) * (i * step) / seconds)
        for i in range(n + 1)
    ]


# ---------------------------------------------------------------------------
# Real draws, replayed
# ---------------------------------------------------------------------------


def test_the_95_second_draw_is_detected():
    """Measured 2026-09-12: 50.0 -> 46.0 C in 95 s, element off.

    About 20x the fastest passive cooling ever recorded on this tank.
    """
    sensor = build()
    feed(sensor, ramp(50.0, 46.0, 95, step=19))

    assert sensor.is_on
    assert sensor.extra_state_attributes["temperature_rate_c_per_min"] > 2.0


def test_a_draw_during_active_heating_is_detected():
    """Measured 2026-09-14: 45.8 -> 41.5 C over 4.6 min with the element ON.

    No heater-state special case: a draw this steep outruns what the element
    adds, so one threshold catches it.
    """
    sensor = build()
    feed(sensor, ramp(45.8, 41.5, 276, step=23))

    assert sensor.is_on


def test_a_long_shower_stays_detected_throughout():
    """Measured 2026-09-15: 49.5 -> 44.0 C over ~9.4 min, with a mid-draw pause.

    Averages 0.585 C/min -- inside the grey band, so it needs the house signal.
    """
    sensor = build(pump="on")
    feed(sensor, ramp(49.5, 46.5, 330, step=30))
    assert sensor.is_on

    # The ~0.5 C recovery blip seen in the real trace at the pause.
    feed(sensor, [(360, 47.0), (390, 47.0)])
    assert sensor.is_on, "a pause mid-shower split one draw in two"

    feed(sensor, [(420, 45.6), (450, 44.0)])
    assert sensor.is_on


# ---------------------------------------------------------------------------
# What must never fire
# ---------------------------------------------------------------------------


def test_fastest_passive_cooling_does_not_fire():
    """Measured worst case: ~8 K/h (0.13 C/min) just after a 64.4 C cutout."""
    sensor = build()
    feed(sensor, ramp(64.4, 63.0, 630, step=30))

    assert not sensor.is_on, "passive cooling was reported as a draw"


def test_settled_cooling_does_not_fire():
    """Measured overnight decay, ~1 K/h."""
    sensor = build()
    feed(sensor, ramp(62.0, 61.0, 3600, step=120))

    assert not sensor.is_on


def test_normal_heating_does_not_fire():
    """A rising tank is not a draw."""
    sensor = build()
    feed(sensor, ramp(50.0, 53.0, 1800, step=60))

    assert not sensor.is_on


def test_a_reporting_gap_cannot_fake_a_plunge():
    """The sample before an outage must not anchor a slope against the one after."""
    sensor = build()
    feed(sensor, [(0, 60.0)])
    sensor._async_add_sample("unavailable", at(60))
    feed(sensor, [(3600, 48.0), (3660, 47.9)])

    assert not sensor.is_on, "an outage was read as a 12 C plunge"


def test_a_single_quantisation_step_does_not_fire():
    """Two samples 5 s apart across one 0.1 C step imply 1.2 C/min from nothing."""
    sensor = build()
    feed(sensor, [(0, 55.0), (5, 54.9)])

    assert not sensor.is_on


# ---------------------------------------------------------------------------
# Release behaviour
# ---------------------------------------------------------------------------


def test_the_draw_clears_once_the_fall_stops():
    """It has to end, and only after the linger window."""
    sensor = build()
    feed(sensor, ramp(50.0, 46.0, 95, step=19))
    assert sensor.is_on

    feed(sensor, [(95 + s, 46.0) for s in range(30, 200, 30)])
    assert sensor.is_on, "cleared before the linger window elapsed"

    feed(sensor, [(95 + s, 46.0) for s in range(210, 480, 30)])
    assert not sensor.is_on
    assert sensor.extra_state_attributes["drawing_since"] is None


# ---------------------------------------------------------------------------
# The external signal corroborates; it does not gate
# ---------------------------------------------------------------------------


def test_a_draw_is_reported_even_when_the_house_signal_is_silent():
    """A draw served from a pressure-tank reserve never engages the pump.

    Measured: 6.5% of pump-confirmed-OFF windows still showed large drops.
    """
    sensor = build(pump="off")
    feed(sensor, ramp(50.0, 46.0, 95, step=19))

    assert sensor.is_on, "the whole-house signal was allowed to veto the tank's own"
    assert sensor.extra_state_attributes["water_in_use_agrees"] is False


def test_a_draw_is_reported_when_the_house_signal_is_unavailable():
    """Blind is not the same as no."""
    sensor = build(pump="unavailable")
    feed(sensor, ramp(50.0, 46.0, 95, step=19))

    assert sensor.is_on
    assert sensor.extra_state_attributes["water_in_use_agrees"] is None


def test_agreement_is_recorded_when_the_house_signal_confirms():
    sensor = build(pump="on")
    feed(sensor, ramp(50.0, 46.0, 95, step=19))

    assert sensor.extra_state_attributes["water_in_use_agrees"] is True


def test_a_pump_starting_late_still_counts_as_agreement():
    """The tap opens before the pressure switch closes."""
    sensor = build(pump="off")
    feed(sensor, ramp(50.0, 47.5, 60, step=20))
    assert sensor.is_on
    assert sensor.extra_state_attributes["water_in_use_agrees"] is False

    sensor.hass.states.set("binary_sensor.house_pump", "on")
    feed(sensor, [(80, 46.8), (100, 46.0)])

    assert sensor.extra_state_attributes["water_in_use_agrees"] is True


def test_the_thresholds_sit_between_the_measured_populations():
    """Guards the two numbers the whole detector rests on.

    Fastest measured passive cooling is 0.13 C/min; the slowest verified draw
    averaged 0.365. Both tiers must stay inside that gap, in the right order.
    """
    assert 0.13 < CORROBORATED_RATE_K_PER_MIN <= 0.365
    assert CORROBORATED_RATE_K_PER_MIN < DRAW_RATE_K_PER_MIN < 1.0


def test_the_grey_band_needs_the_house_signal():
    """A 0.5 C/min fall is real but arguable -- alone it must not fire."""
    sensor = build(pump="off")
    feed(sensor, ramp(50.0, 48.5, 180, step=30))
    assert not sensor.is_on, "a grey-band fall fired with no corroboration"

    corroborated = build(pump="on")
    feed(corroborated, ramp(50.0, 48.5, 180, step=30))
    assert corroborated.is_on, "a grey-band fall failed to fire with corroboration"


def test_an_unambiguous_fall_never_needs_the_house_signal():
    """The tank's own evidence outranks a signal that cannot see this tank."""
    sensor = build(pump="off")
    feed(sensor, ramp(50.0, 46.0, 95, step=19))
    assert sensor.is_on



# ---------------------------------------------------------------------------
# It must clear on a timer, not only on the next reading
# ---------------------------------------------------------------------------


def test_the_draw_clears_on_a_timer_with_no_further_samples(fake_clock):
    """The downstairs sensor reports on ~0.5 C of change.

    Once the tank goes flat the next sample can be twenty minutes away, so a
    detector that can only clear on an incoming reading would report a draw
    running for all of it.
    """
    sensor = build()
    feed(sensor, ramp(50.0, 46.0, 95, step=19))
    assert sensor.is_on
    assert fake_clock.pending is not None, "no timer armed while a draw was running"

    # No further temperature samples at all -- just the timer expiring, twice:
    # once while the fall is still inside the lookback window, then once after.
    fake_clock.fire(sensor)
    if sensor.is_on and fake_clock.pending is not None:
        fake_clock.fire(sensor)

    assert not sensor.is_on, "the draw could only have cleared on a new reading"


def test_the_timer_is_cancelled_when_the_entity_goes_away(fake_clock):
    """Nothing should fire into a removed entity."""
    sensor = build()
    feed(sensor, ramp(50.0, 46.0, 95, step=19))
    assert fake_clock.pending is not None

    sensor._cancel_linger()

    assert fake_clock.pending is None


# ---------------------------------------------------------------------------
# Startup and ordering
# ---------------------------------------------------------------------------


async def test_a_stale_startup_reading_cannot_anchor_a_fall(monkeypatch):
    """After a restart the last reading can be hours old.

    Drives the REAL async_added_to_hass seeding path: stamping that reading with
    wall-clock "now" instead of its own timestamp would leave it sitting in the
    lookback window, to pair with the next genuine sample and fabricate a plunge.
    A version of this test that called _async_add_sample directly passed against
    the bug, because it never entered the code under test.
    """
    monkeypatch.setattr(
        binary_sensor_module, "async_track_state_change_event", lambda *a, **k: (lambda: None)
    )
    now = dt_util.utcnow()
    sensor = build()
    # The tank was at 60 C four hours ago; nothing has reported since.
    sensor.hass.states.set("sensor.upstairs_temperature", ("60.0", now - timedelta(hours=4)))

    await sensor.async_added_to_hass()

    # First genuine reading after the restart: the tank simply cooled.
    sensor._async_add_sample(48.0, now + timedelta(seconds=60))

    assert not sensor.is_on, "a four-hour-old reading was treated as current"


def test_an_out_of_order_sample_does_not_lose_a_real_fall():
    """The newest sample must stay newest, or the fall is silently dropped."""
    sensor = build()
    feed(sensor, [(0, 50.0), (95, 46.0)])
    assert sensor.is_on
    rate = sensor.extra_state_attributes["temperature_rate_c_per_min"]

    # A late-arriving duplicate from before the fall must not disturb it.
    sensor._async_add_sample(50.0, at(10))

    assert sensor.is_on
    assert sensor.extra_state_attributes["temperature_rate_c_per_min"] == rate


# ---------------------------------------------------------------------------
# Held-out data: a real shower the detector had never seen
# ---------------------------------------------------------------------------


# Verbatim from recorder history, upstairs tank, 2026-09-17. The tank sat flat
# at 44.2-44.4 C for fifty minutes, then a shower started. Note the 79-second
# sensor dropout in the middle of it -- this sensor does that.
REAL_SHOWER = [
    (0, 44.4), (5, 44.3), (10, 44.2), (20, 44.0), (25, 43.9), (30, 43.8), (35, 43.5),
    # sensor drops out here for 79 s, then returns 2.3 C lower
    (133, "unavailable"),
    (177, 41.2), (190, 41.1), (195, 41.0), (205, 40.9), (210, 40.8), (225, 40.7),
    (230, 40.6), (245, 40.5), (255, 40.4), (260, 40.2), (275, 40.1), (290, 40.0),
    (295, 39.9), (310, 39.8), (320, 39.7), (325, 39.6), (340, 39.5), (345, 39.4),
]


def test_the_real_2026_09_17_shower_is_detected_on_its_opening():
    """The opening of a real shower must fire on the tank's evidence alone.

    Measured: 44.4 -> 43.5 C in 35 s, i.e. 1.54 C/min, nearly twice the certain
    threshold. A 45-second span floor threw that away and pushed detection 3m40s
    later into the corroborated tier -- where it depended on a whole-house pump
    signal that was unavailable at that moment. With no pump agreeing, this must
    still fire, and fire on the opening.
    """
    sensor = build(pump="unavailable")
    feed(sensor, [(s, t) for s, t in REAL_SHOWER if s <= 35])

    assert sensor.is_on, "the unambiguous opening of a real shower was missed"
    assert sensor.extra_state_attributes["water_in_use_agrees"] is None
    assert sensor.extra_state_attributes["temperature_rate_c_per_min"] >= 1.5


def test_the_real_shower_is_still_seen_after_the_sensor_drops_out():
    """A dropout mid-draw restarts the detector; it must pick the draw back up.

    Clearing history on unavailable is deliberate -- a gap is not a plunge -- so
    the cost is a cold restart, not a missed draw.
    """
    sensor = build(pump="on")
    feed(sensor, [(s, t) for s, t in REAL_SHOWER if s <= 35])
    sensor._async_add_sample("unavailable", at(133))
    feed(sensor, [(s, t) for s, t in REAL_SHOWER if s >= 177])

    assert sensor.is_on, "the draw was not picked back up after the dropout"


def test_the_fifty_flat_minutes_before_it_never_fire():
    """The same trace's quiet hour: 0.1 C dither at 44.2-44.4 must stay silent."""
    sensor = build(pump="on")
    dither = [44.2, 44.3, 44.2, 44.3, 44.4, 44.3, 44.4, 44.3, 44.2, 44.3, 44.4, 44.3]
    feed(sensor, [(i * 90, v) for i, v in enumerate(dither * 3)])

    assert not sensor.is_on, "sensor dither was reported as a draw"
