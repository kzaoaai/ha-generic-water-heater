"""Constants and helpers shared by the water heater entity modules."""
import logging
from datetime import timedelta
from . import (
    CONF_ENABLE_FORECAST_PREHEAT,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_MIN_KWH,
    CONF_FORECAST_PREHEAT_TARGET,
)

# Same logger name as water_heater.py, so log filtering is unchanged.
_LOGGER = logging.getLogger(f"{__package__}.water_heater")

# How long after Home Assistant finishes starting the eco gate is not allowed to
# park a tank. At startup the template's source entities may not exist yet, and
# a sensor with a delay_on reads a plausible "off" until the delay passes -- on
# 2026-10-02 a PV-excess sensor took ~70 s after load to report its real value.
# A condition that cannot be evaluated is not a condition that is false. The
# window is bounded so a template whose source never returns cannot bypass eco.
STARTUP_GRACE = timedelta(minutes=2)

# How long a switch command of ours may take to be reported back. A cloud relay
# reported an OFF 11 s after it was sent on 2026-10-02; this leaves headroom
# without letting a stale entry swallow a person's flip much later.
OWN_ECHO_WINDOW = timedelta(seconds=30)

# The surplus boost's anti-cycling bound. A surplus detector that judges
# "surplus" from the battery sees the heater's own draw: when the element needs
# more than the spare PV, the pack discharges, surplus clears a few minutes
# later, the element stops, the pack refills and surplus returns. Seen
# 2026-10-18 16:16-17:24 as six on/off cycles of 2-3 minutes. So a boost that
# heated and lost its surplus within SHORT_RUN is not tried again for LOCKOUT.
# Heating to the normal target is never locked out.
SURPLUS_BOOST_SHORT_RUN = timedelta(minutes=10)
SURPLUS_BOOST_LOCKOUT = timedelta(minutes=30)

# How long a disinfection cycle that does not wait for eco ("Once", "As needed")
# may stand the eco policy down. It used to share the manual-pause bound
# (smart_eco_manual_off_resume_hours, 3 h on a real 200 L / 2 kW tank), which is
# about the time to heat from cold to 60 C, before the one-hour hold even
# starts: the bypass ran out short of 60 C and the cycle fell back to waiting
# for the eco condition, parked off, until its 3-day give-up. 8 h covers heat-up
# plus hold with margin while still bounding a dead element.
DISINFECTION_BYPASS_HOURS = 8

# How long the temperature sensor or the heater switch may be unavailable before
# it is raised in Settings > Repairs. Short dropouts are routine (a relay here
# drops off the network many times a day and comes straight back); ten minutes
# is not, and while the sensor is down the failsafe holds the tank off.
OUTAGE_REPAIR_AFTER = timedelta(minutes=10)
# How long the temperature reading may be missing before the failsafe is logged
# as a warning. The failsafe itself acts at once; only the log waits. On a
# restart a sensor whose integration loads later reports unavailable, then
# unknown, then its reading -- about a minute on a real install -- and that
# routine gap was a warning on every restart.
FAILSAFE_WARN_AFTER = timedelta(minutes=2)
# How long a heater may go without a first reading after it loads (on a Home
# Assistant start: after the start has finished) before that counts as a lost
# reading. "No reading yet" is deliberately not the failsafe: setup runs a
# control pass before the sensor is read, and a sensor whose integration loads
# later needs a moment (about a minute on a real install). But it stayed "not
# yet" for ever: an element found on at a restart ran on the tank's own
# thermostat alone until the sensor's state next changed, and the state of a
# sensor that is gone never changes.
FIRST_READING_WITHIN = timedelta(minutes=2)
# How long the heater switch may be unavailable before the heater itself shows
# unavailable. The relays here drop off the network for seconds at a time, many
# times a week (7 days on a real install: 10 drops, none over a minute), and
# each drop used to flash the heater unavailable on dashboards, in HomeKit and
# to the load balancer. The relay holds its state through a drop; commands sent
# meanwhile fail and the next control pass retries them.
SWITCH_UNAVAILABLE_AFTER = timedelta(minutes=2)
# The temperature a disinfection cycle is heading for. Mirrors
# sensor.DISINFECTION_TEMP_C, which cannot be imported here without a cycle.
DISINFECTION_GOAL_C = 60.0


def _forecast_preheat_config(data: dict) -> dict | None:
    """Return the pre-heat settings when it is on and complete, else None."""
    if not data.get(CONF_ENABLE_FORECAST_PREHEAT):
        return None
    entity = (data.get(CONF_FORECAST_ENTITY) or "").strip()
    try:
        min_kwh = float(data.get(CONF_FORECAST_MIN_KWH))
        target = float(data.get(CONF_FORECAST_PREHEAT_TARGET))
    except (TypeError, ValueError):
        return None
    if not entity:
        return None
    return {"entity": entity, "min_kwh": min_kwh, "target": target}
ISSUE_TEMPERATURE_SENSOR = "temperature_sensor_unavailable"
ISSUE_SWITCH = "heater_switch_unavailable"
ISSUE_DISINFECTION_GAVE_UP = "disinfection_gave_up"
