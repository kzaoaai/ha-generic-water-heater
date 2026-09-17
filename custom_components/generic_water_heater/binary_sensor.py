"""Binary sensor platform for Generic Water Heater.

Reports when hot water is being drawn FROM THIS TANK, which a whole-house flow
or pump signal cannot tell you: that signal fires for a cold tap, an irrigation
valve, or another appliance entirely. What identifies this tank is its own
temperature falling far faster than it physically can by standing loss.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import CONF_NAME, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
import homeassistant.util.dt as dt_util

from . import (
    CONF_HEATER,
    CONF_SENSOR,
    CONF_WATER_IN_USE_ENTITY,
    DOMAIN,
    async_resolve_heater_device,
)

_LOGGER = logging.getLogger(__name__)

# Two tiers, because the measured draws do not all separate cleanly from
# cooling on rate alone. Six verified draws on a real tank averaged 0.365, 0.585,
# 0.609, 0.767, 0.93 and 2.53 C/min; the fastest passive cooling ever recorded
# here is 0.13 C/min, in the minutes just after a thermostat cutout, an order of
# magnitude slower once settled.
#
# A fall at or above the CERTAIN rate cannot be cooling -- roughly 6x the fastest
# passive rate -- so it stands on its own.
DRAW_RATE_K_PER_MIN = 0.8

# Between the two lies a grey band that holds most real draws but sits close
# enough to post-cutout cooling to be arguable. This is where a whole-house
# water signal is worth having: it cannot say WHICH fixture ran, but paired with
# a fall this tank should not be showing, it settles the question. Using it only
# here means it can break a tie but never veto an unambiguous signal.
CORROBORATED_RATE_K_PER_MIN = 0.35

# There is deliberately NO separate release RATE. Any fall still in view has
# already cleared MIN_DROP_C inside LOOKBACK, so it is at least
# MIN_DROP_C / LOOKBACK = 0.27 C/min -- a release threshold below that could
# never be reached, and one above it would just be a second trigger. Staying on
# needs only a qualifying fall still in the window, which is the hysteresis:
# 0.8 C in three minutes to stay, 0.35-0.8 C/min to start.

# A rate alone is not enough on a fast-reporting sensor: two samples 5 s apart
# straddling a 0.1 K quantisation step imply 1.2 K/min from nothing. Require a
# real excursion as well -- and MIN_DROP_C is what actually rejects that noise,
# since 0.8 C is eight quantisation steps in one direction.
#
# MIN_SPAN is therefore only a sanity floor, and at 45 s it was costing real
# detections. A measured shower on 2026-09-17 opened at 1.54 C/min and cleared
# the drop in 35 s: the span floor alone delayed detection by 3m40s and pushed
# what was an unambiguous fall out of the certain tier into needing the
# whole-house signal, which happened to be unavailable at the time.
MIN_DROP_C = 0.8
MIN_SPAN = timedelta(seconds=45)

# How far back to look for the start of a fall. Deliberately short: a long
# window keeps qualifying after the fall stops, because an old high sample
# averaged against the present still clears the rate. Three minutes is longer
# than the coarsest useful reporting interval during a draw (a 0.5 C
# delta-triggered sensor reports every ~37 s at the certain rate) and short
# enough that the tank going flat drops the fall out of view on its own.
LOOKBACK = timedelta(minutes=3)

# Keep reporting for this long after the fall stops, so a pause mid-shower does
# not split one draw into two.
LINGER = timedelta(minutes=2)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Hot Water In Use sensor, if an in-use entity was configured."""
    data = {**entry.data, **getattr(entry, "options", {})}

    water_in_use_entity = (data.get(CONF_WATER_IN_USE_ENTITY) or "").strip() or None
    if water_in_use_entity is None:
        # Nothing configured: create no entity at all, matching how the other
        # optional entities in this integration are gated.
        return

    source_sensor_entity_id = data.get(CONF_SENSOR)
    if not source_sensor_entity_id:
        return

    device_identifiers, device_has_name = async_resolve_heater_device(
        hass, data.get(CONF_HEATER)
    )

    async_add_entities(
        [
            HotWaterInUseBinarySensor(
                name=data.get(CONF_NAME),
                source_sensor_entity_id=source_sensor_entity_id,
                water_in_use_entity_id=water_in_use_entity,
                device_identifier=entry.entry_id,
                device_identifiers=device_identifiers,
                device_has_name=device_has_name,
            )
        ]
    )


class HotWaterInUseBinarySensor(BinarySensorEntity):
    """Detect hot water being drawn from this tank, from its own temperature.

    The configured water-in-use entity is corroboration, not a gate. It is a
    whole-house signal -- it cannot say which fixture, or whether the water was
    hot -- and gating on it would both miss draws served from a pressure-tank
    reserve and go blind whenever it is unavailable. The temperature fall is
    what identifies THIS tank, so that is what decides.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Hot Water In Use"
    _attr_icon = "mdi:shower"

    def __init__(
        self,
        name: str | None,
        source_sensor_entity_id: str,
        water_in_use_entity_id: str,
        device_identifier: str,
        device_identifiers,
        device_has_name: bool = False,
    ) -> None:
        """Initialize the draw detector."""
        self._source_sensor_entity_id = source_sensor_entity_id
        self._water_in_use_entity_id = water_in_use_entity_id
        self._device_identifier = device_identifier
        self._device_identifiers = device_identifiers

        self._samples: list[tuple[datetime, float]] = []
        self._attr_is_on = False
        self._drawing_since: datetime | None = None
        self._last_qualified_at: datetime | None = None
        self._rate: float | None = None
        self._observed_drop: float | None = None
        self._corroborated: bool | None = None
        self._linger_timer = None

        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        if name and not device_has_name:
            self._attr_name = f"{name} Hot Water In Use"
            self._attr_has_entity_name = False

        self._attr_unique_id = f"{DOMAIN}_{device_identifier}_hot_water_in_use"

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}
        return {"identifiers": {(DOMAIN, self._device_identifier)}}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the numbers behind the state, so it can be argued with."""
        return {
            "water_in_use_entity": self._water_in_use_entity_id,
            "water_in_use_agrees": self._corroborated,
            "temperature_rate_c_per_min": (
                round(self._rate, 3) if self._rate is not None else None
            ),
            "observed_drop_c": (
                round(self._observed_drop, 2)
                if self._observed_drop is not None
                else None
            ),
            "drawing_since": (
                self._drawing_since.isoformat() if self._drawing_since else None
            ),
            "trigger_rate_c_per_min": DRAW_RATE_K_PER_MIN,
            "corroborated_trigger_rate_c_per_min": CORROBORATED_RATE_K_PER_MIN,
            "caveat": (
                "Inferred from this tank's temperature falling faster than it can "
                "by standing loss. A short draw, or one that only cancels heating, "
                "is not detected."
            ),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to the temperature sensor."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_sensor_entity_id],
                self._async_source_sensor_changed,
            )
        )

        source_state = self.hass.states.get(self._source_sensor_entity_id)
        if source_state is not None:
            # The state's OWN timestamp, not wall clock. After a restart the
            # last reading can be hours old; stamping it "now" would let it
            # anchor a slope against the next real sample and fabricate a fall.
            self._async_add_sample(source_state.state, source_state.last_updated)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the linger timer so nothing fires after the entity is gone."""
        self._cancel_linger()

    @callback
    def _cancel_linger(self) -> None:
        """Cancel any pending linger re-evaluation."""
        if self._linger_timer is not None:
            self._linger_timer()
            self._linger_timer = None

    @callback
    def _schedule_linger_check(self) -> None:
        """Re-evaluate when the linger window expires.

        Without this the sensor can only ever clear on the next incoming
        temperature sample -- and the downstairs sensor reports on ~0.5 C of
        change, so once the tank goes flat the next sample may be twenty minutes
        away. The draw would read as still running for all of it.
        """
        self._cancel_linger()
        self._linger_timer = async_call_later(
            self.hass, LINGER.total_seconds() + 1, self._async_linger_expired
        )

    @callback
    def _async_linger_expired(self, _now) -> None:
        """Re-evaluate on the timer rather than waiting for a sample."""
        self._linger_timer = None
        self._evaluate(dt_util.utcnow())
        self.async_write_ha_state()

    @callback
    def _async_source_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a temperature update."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._async_add_sample(new_state.state, event.time_fired or dt_util.utcnow())
        self.async_write_ha_state()

    @callback
    def _async_add_sample(self, state_value: Any, when: datetime) -> None:
        """Record a sample and re-evaluate."""
        if state_value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # A gap is not a plunge. Drop the history rather than let the sample
            # before an outage anchor a slope against the one after it.
            self._samples.clear()
            self._evaluate(when)
            return

        try:
            temperature = float(state_value)
        except (TypeError, ValueError):
            return

        if self._samples and when <= self._samples[-1][0]:
            # _steepest_fall treats the last element as the newest. A sample
            # that arrives out of order would break that and silently lose a
            # real fall, reporting no rate at all rather than erroring.
            _LOGGER.debug(
                "%s: ignoring out-of-order sample at %s (newest is %s)",
                self.name,
                when,
                self._samples[-1][0],
            )
            return

        self._samples.append((when, temperature))
        self._evaluate(when)

    @callback
    def _steepest_fall(self, now: datetime) -> tuple[float | None, float | None]:
        """Return (rate C/min, drop C) for the steepest qualifying fall in view.

        Every earlier sample is tried as the start, and the steepest fall that
        also clears the minimum span and drop wins. A single pair would be at the
        mercy of where the sensor happened to report; this finds the excursion.
        """
        if len(self._samples) < 2:
            return None, None

        latest_at, latest = self._samples[-1]
        best_rate: float | None = None
        best_drop: float | None = None

        for earlier_at, earlier in self._samples[:-1]:
            span = latest_at - earlier_at
            if span < MIN_SPAN:
                continue
            drop = earlier - latest
            if drop < MIN_DROP_C:
                continue
            rate = drop / (span.total_seconds() / 60)
            if best_rate is None or rate > best_rate:
                best_rate = rate
                best_drop = drop

        return best_rate, best_drop

    @callback
    def _corroboration(self) -> bool | None:
        """Return whether the external in-use signal agrees, or None if it cannot say."""
        state = self.hass.states.get(self._water_in_use_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return state.state == STATE_ON

    @callback
    def _evaluate(self, now: datetime) -> None:
        """Decide whether this tank is currently giving up hot water."""
        # Age the window out HERE rather than only when a sample arrives. The
        # linger timer re-evaluates with no new sample, and without this the
        # fall that started the draw would stay in view for ever, re-arming the
        # timer on every expiry and latching the sensor on permanently.
        cutoff = now - LOOKBACK
        self._samples = [s for s in self._samples if s[0] >= cutoff]

        rate, drop = self._steepest_fall(now)
        self._rate = rate
        self._observed_drop = drop

        agrees = self._corroboration()

        if self._attr_is_on:
            qualifies = rate is not None
        elif rate is None:
            qualifies = False
        elif rate >= DRAW_RATE_K_PER_MIN:
            qualifies = True
        else:
            # Grey band: only the external signal can settle it.
            qualifies = rate >= CORROBORATED_RATE_K_PER_MIN and agrees is True

        if qualifies:
            self._last_qualified_at = now
            self._schedule_linger_check()
            if not self._attr_is_on:
                self._attr_is_on = True
                self._drawing_since = now
                self._corroborated = agrees
                _LOGGER.debug(
                    "%s: hot water draw detected (%.2f C/min over %.2f C)",
                    self.name,
                    rate,
                    drop,
                )
            elif self._corroborated is not True and agrees is not None:
                # Keep looking for agreement for as long as the draw lasts: a
                # pump can start after the tap does.
                self._corroborated = agrees or self._corroborated
            return

        if not self._attr_is_on:
            return

        if (
            self._last_qualified_at is not None
            and now - self._last_qualified_at < LINGER
        ):
            # Still inside the linger window -- a pause mid-shower is one draw.
            return

        _LOGGER.debug("%s: hot water draw ended", self.name)
        self._cancel_linger()
        self._attr_is_on = False
        self._drawing_since = None
        self._last_qualified_at = None
        self._corroborated = None
