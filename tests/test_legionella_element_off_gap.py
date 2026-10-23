"""A long silent gap counts toward a hold only if the element stayed off.

MAX_CREDITED_GAP refuses credit for unobserved time. Its one exception
(MAX_ELEMENT_OFF_GAP): with the element off for the whole gap and both readings
at or above 60 C, the tank can only have cooled, so it stayed at or above 60 C.
Seen 2026-10-23: a sensor reporting only on ~0.5 C steps went silent for 34+ min
with a tank idle at 61 C after a surplus boost, and the hold stalled.
"""

from datetime import timedelta

from freezegun import freeze_time
import pytest
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.generic_water_heater import CONF_ENABLE_LEGIONELLA_SENSOR
from custom_components.generic_water_heater.sensor import (
    MAX_CREDITED_GAP,
    MAX_ELEMENT_OFF_GAP,
    LegionellaRiskSensor,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)
from tests.test_legionella import BASE, at, feed

LONG = MAX_CREDITED_GAP.total_seconds() / 60 + 15  # minutes, past the normal cap


def build(element_off_since):
    return LegionellaRiskSensor(
        name="Upstairs",
        source_sensor_entity_id="sensor.upstairs_temperature",
        device_identifier="01JQ0000000000000000UPSTRS",
        device_identifiers=None,
        interval_days=7,
        element_off_since=element_off_since,
    )


def test_a_long_gap_with_the_element_off_is_credited():
    sensor = build(lambda since: True)
    feed(sensor, [(0, 61.0), (LONG, 60.5)])
    assert sensor._hold_seconds == pytest.approx(LONG * 60)


def test_the_check_is_asked_about_the_start_of_the_gap():
    asked = []
    sensor = build(lambda since: asked.append(since) or True)
    feed(sensor, [(0, 61.0), (LONG, 60.5)])
    assert asked == [at(0)]


def test_a_long_gap_with_the_element_on_is_not_credited():
    sensor = build(lambda since: False)
    feed(sensor, [(0, 61.0), (LONG, 60.5)])
    assert sensor._hold_seconds == 0.0


def test_a_long_gap_ending_below_60_is_not_credited():
    """Element off, but the tank left 60 C somewhere in the gap."""
    sensor = build(lambda since: True)
    feed(sensor, [(0, 61.0), (LONG, 59.5)])
    assert sensor._hold_seconds == 0.0


def test_an_element_off_gap_past_its_own_bound_is_not_credited():
    sensor = build(lambda since: True)
    too_long = MAX_ELEMENT_OFF_GAP.total_seconds() / 60 + 1
    feed(sensor, [(0, 61.0), (too_long, 60.5)])
    # Credited, this gap would complete the hour and reset the counter, so the
    # counter alone cannot tell; the completion stamp can.
    assert sensor._last_disinfection_at is None
    assert sensor._hold_seconds == 0.0


def test_the_seen_case_completes_the_hold():
    """10.8 min banked while heating, then the tank coasts, reporting rarely."""
    sensor = build(lambda since: True)
    feed(sensor, [(0, 60.03), (5, 60.57), (10.8, 61.08), (55, 60.57), (64, 60.03)])
    assert sensor._last_disinfection_at == at(64)


async def _setup(hass):
    # Re-stamp the switch inside the frozen clock: the fixture set it off at
    # the real time, which is later than the frozen one.
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
    hass.states.async_set(UPSTAIRS_SENSOR, "61.0", {"device_class": "temperature"})
    # Normal target 40: the tank is satisfied, so the heater leaves the element off.
    entry = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, target_temp=40.0,
        **{CONF_ENABLE_LEGIONELLA_SENSOR: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _progress(hass):
    return float(hass.states.get("sensor.upstairs_disinfection_hold_progress").state)


async def test_end_to_end_the_switch_state_decides(hass, world):  # noqa: F811
    with freeze_time(BASE) as frozen:
        await _setup(hass)
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
        frozen.tick(timedelta(minutes=1))
        hass.states.async_set(UPSTAIRS_SENSOR, "61.1", {"device_class": "temperature"})
        await hass.async_block_till_done()

        frozen.tick(timedelta(minutes=LONG))
        hass.states.async_set(UPSTAIRS_SENSOR, "60.6", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert _progress(hass) == pytest.approx(1 + LONG, abs=0.1)


async def test_end_to_end_a_switch_flip_inside_the_gap_is_not_credited(hass, world):  # noqa: F811
    with freeze_time(BASE) as frozen:
        await _setup(hass)
        frozen.tick(timedelta(minutes=1))
        hass.states.async_set(UPSTAIRS_SENSOR, "61.1", {"device_class": "temperature"})
        await hass.async_block_till_done()
        banked = _progress(hass)

        # Someone flips the element on and off again during the silence.
        frozen.tick(timedelta(minutes=10))
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        frozen.tick(timedelta(minutes=1))
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_OFF)
        await hass.async_block_till_done()

        frozen.tick(timedelta(minutes=LONG))
        hass.states.async_set(UPSTAIRS_SENSOR, "60.6", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert _progress(hass) == pytest.approx(banked)


async def test_the_switch_check_reads_off_and_unchanged_since(hass):
    """The production check: off now, and last changed no later than asked."""
    from homeassistant.const import STATE_UNAVAILABLE

    from custom_components.generic_water_heater.sensor import _element_off_since

    check = _element_off_since(hass, "switch.element")
    with freeze_time(BASE) as frozen:
        assert check(at(0)) is False, "a missing switch is not off"

        hass.states.async_set("switch.element", STATE_ON)
        frozen.tick(timedelta(minutes=10))
        assert check(at(5)) is False, "on is not off"

        hass.states.async_set("switch.element", STATE_OFF)  # changed at 10
        assert check(at(5)) is False, "it was on for part of the gap"
        assert check(at(10)) is True
        assert check(at(15)) is True

        frozen.tick(timedelta(minutes=10))
        hass.states.async_set("switch.element", STATE_UNAVAILABLE)
        assert check(at(15)) is False, "unavailable is not off"

    assert _element_off_since(hass, None)(at(0)) is False
