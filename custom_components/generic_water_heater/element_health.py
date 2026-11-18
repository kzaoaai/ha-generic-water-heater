"""Element health: does the meter agree with the relay?

Read-only. The meter (`power_sensor`) is published for other integrations and
never used for control (see CLAUDE.md); this only reports when what the relay
was told and what the circuit draws disagree:

- drawing while off: the relay is off but the circuit draws power. A welded
  relay, a second switch in parallel, or the wrong meter configured.
- no draw while on: the relay is on, the tank is well below where any
  mechanical thermostat would cut it, and the circuit draws nothing. A dead
  element, a tripped breaker or thermostat, or a relay that did not close.

Pure logic, no Home Assistant imports, like fleet.py: the entity in
binary_sensor.py feeds it and holds the timing.
"""
from __future__ import annotations

from datetime import timedelta

FAULT_DRAWING_WHILE_OFF = "drawing_while_off"
FAULT_NO_DRAW_WHILE_ON = "no_draw_while_on"

# Above this the circuit is drawing: well clear of a meter's standby noise and
# far below a 1.2-2 kW element.
DRAW_W = 100.0
# Below this an element that should be on is not.
NO_DRAW_W = 20.0

# The tank's own mechanical thermostat legitimately opens the circuit with the
# relay closed -- PERFORMANCE runs to it on purpose. Those cutouts sit around
# 60 C or above, so "no draw" is only a fault while the tank is at least
# COLD_MARGIN_C below both its target and NO_CUTOUT_BELOW_C.
NO_CUTOUT_BELOW_C = 50.0
COLD_MARGIN_C = 5.0

# How long a disagreement must last. A meter reports after the relay moves, so
# a short lag is normal; an element left on and drawing nothing for ten minutes
# is not. Drawing while off is cheaper to confirm and costlier to miss.
HOLD = {
    FAULT_DRAWING_WHILE_OFF: timedelta(minutes=3),
    FAULT_NO_DRAW_WHILE_ON: timedelta(minutes=10),
}


def element_fault(
    switch_on: bool | None,
    power_w: float | None,
    temperature: float | None,
    target: float | None,
) -> str | None:
    """Which disagreement, if any, the current readings show (not yet held).

    None for every input that is unknown: no verdict without evidence.
    """
    if switch_on is None or power_w is None:
        return None
    if not switch_on:
        return FAULT_DRAWING_WHILE_OFF if power_w >= DRAW_W else None
    if power_w >= NO_DRAW_W or temperature is None or target is None:
        return None
    if temperature <= min(target, NO_CUTOUT_BELOW_C) - COLD_MARGIN_C:
        return FAULT_NO_DRAW_WHILE_ON
    return None


# Home Assistant's own power units (UnitOfPower), spelled exactly, plus the
# case variants that cannot mean anything else: no other prefix is "k", and
# nothing but the watt is a bare "w". "mw" and "Mw" are left out on purpose:
# milliwatt and megawatt differ by 1e9 and a guess would be wrong either way.
# This module stays free of Home Assistant imports, so the table is spelled
# out here rather than taken from its unit converter.
_WATTS_PER_UNIT = {
    "mW": 0.001,
    "W": 1.0,
    "w": 1.0,
    "kW": 1000.0,
    "kw": 1000.0,
    "KW": 1000.0,
    "Kw": 1000.0,
    "MW": 1_000_000.0,
    "GW": 1_000_000_000.0,
    "TW": 1_000_000_000_000.0,
    "BTU/h": 0.29307107,
}


def to_watts(value: float, unit: str | None) -> float:
    """Normalise a power reading to watts.

    A unit nobody can interpret (including none) is taken as watts, as before:
    the meter is read-only evidence, and refusing a verdict would need every
    consumer to cope with a missing number. Surrounding whitespace is ignored,
    since a stray space made kW read as W, a thousand-fold error.
    """
    if not isinstance(unit, str):
        return value
    return value * _WATTS_PER_UNIT.get(unit.strip(), 1.0)
