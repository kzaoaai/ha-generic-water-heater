"""Element Fault: the meter and the relay disagree for long enough (read-only)."""

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.const import STATE_OFF, STATE_ON
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater.element_health import (
    FAULT_DRAWING_WHILE_OFF,
    FAULT_NO_DRAW_WHILE_ON,
    element_fault,
    to_watts,
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

FAULT = "binary_sensor.upstairs_element_fault"


# --- the decision, pure -------------------------------------------------------


@pytest.mark.parametrize(
    ("switch_on", "power", "temp", "target", "expected"),
    [
        (False, 1500.0, 40.0, 60.0, FAULT_DRAWING_WHILE_OFF),
        (False, 50.0, 40.0, 60.0, None),  # standby noise is not drawing
        (True, 0.0, 40.0, 60.0, FAULT_NO_DRAW_WHILE_ON),
        (True, 1800.0, 40.0, 60.0, None),
        # Near a mechanical cutout no draw is normal (performance runs to it).
        (True, 0.0, 58.0, 60.0, None),
        (True, 0.0, 46.0, 60.0, None),  # within COLD_MARGIN of 50 C
        (True, 0.0, 40.0, 44.0, None),  # within COLD_MARGIN of its target
        (None, 1500.0, 40.0, 60.0, None),  # switch unknown: no verdict
        (False, None, 40.0, 60.0, None),  # meter unknown: no verdict
        (True, 0.0, None, 60.0, None),
    ],
)
def test_the_decision(switch_on, power, temp, target, expected):
    assert element_fault(switch_on, power, temp, target) == expected


def test_kilowatts_are_watts():
    assert to_watts(1.5, "kW") == 1500.0
    assert to_watts(1500.0, "W") == 1500.0


@pytest.mark.parametrize(
    ("unit", "watts"),
    [
        ("mW", 0.001),
        ("W", 1.0),
        ("kW", 1000.0),
        ("MW", 1_000_000.0),
        ("GW", 1e9),
        ("TW", 1e12),
        ("BTU/h", 0.29307107),
        ("kw", 1000.0),
        ("KW", 1000.0),
        ("Kw", 1000.0),
        ("w", 1.0),
        (" kW ", 1000.0),
        ("W\n", 1.0),
    ],
)
def test_every_power_unit_home_assistant_has_is_read_at_its_own_scale(unit, watts):
    """A meter in mW, GW or TW, or a kW with a stray space or capital, was a
    thousand-fold misread (taken as watts)."""
    assert to_watts(1.0, unit) == pytest.approx(watts)


def test_milliwatt_and_megawatt_are_never_confused():
    assert to_watts(5.0, "mW") == pytest.approx(0.005)
    assert to_watts(5.0, "MW") == 5_000_000.0


@pytest.mark.parametrize("unit", ["mw", "Mw", "mW?", "", "horsepower", None, 5])
def test_a_unit_nobody_can_interpret_is_still_taken_as_watts(unit):
    """Guard: ambiguous spellings are not guessed, and the old default stays.

    That includes a unit attribute that is not text at all.
    """
    assert to_watts(42.0, unit) == 42.0


# --- the entity --------------------------------------------------------------


async def tick(hass, frozen, minutes):
    frozen.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_drawing_while_off_is_reported_after_three_minutes(hass, world):
    with freeze_time(TRIGGER) as frozen:
        _meter(hass, "0")
        await _setup(hass, power_sensor=METER)  # PV off: eco parks it, relay off
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
        assert hass.states.get(FAULT).state == STATE_OFF

        _meter(hass, "1500")
        await hass.async_block_till_done()
        await tick(hass, frozen, 2)
        assert hass.states.get(FAULT).state == STATE_OFF, "a meter lag is not a fault"
        await tick(hass, frozen, 2)

        state = hass.states.get(FAULT)
        assert state.state == STATE_ON
        assert state.attributes["fault"] == FAULT_DRAWING_WHILE_OFF
        assert state.attributes["fault_since"] == TRIGGER.isoformat()

        _meter(hass, "0")
        await hass.async_block_till_done()
        assert hass.states.get(FAULT).state == STATE_OFF


async def test_no_draw_while_heating_is_reported_after_ten_minutes(hass, world):
    with freeze_time(TRIGGER) as frozen:
        _meter(hass, "0")
        await _setup(hass, power_sensor=METER)
        hass.states.async_set(PV_EXCESS, STATE_ON)  # heats at 40 C toward 60 C
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_ON

        await tick(hass, frozen, 9)
        assert hass.states.get(FAULT).state == STATE_OFF
        await tick(hass, frozen, 2)
        assert hass.states.get(FAULT).state == STATE_ON
        assert hass.states.get(FAULT).attributes["fault"] == FAULT_NO_DRAW_WHILE_ON

        # The element draws: the fault clears at once.
        _meter(hass, "1.9", unit="kW")
        await hass.async_block_till_done()
        assert hass.states.get(FAULT).state == STATE_OFF


async def test_it_never_touches_the_switch(hass, world):
    with freeze_time(TRIGGER) as frozen:
        _meter(hass, "1500")
        await _setup(hass, power_sensor=METER)
        world["turn_on"].clear()
        world["turn_off"].clear()
        await tick(hass, frozen, 5)
        assert hass.states.get(FAULT).state == STATE_ON
    assert world["turn_on"] == [] and world["turn_off"] == []


async def test_no_meter_no_sensor(hass, world):
    await _setup(hass)
    assert hass.states.get(FAULT) is None
