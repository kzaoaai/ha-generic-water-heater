"""Sensor platform for Generic Water Heater."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorExtraStoredData,
)
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from . import (
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_ECO_TEMPLATE,
    CONF_HEATER,
    CONF_LEGIONELLA_INTERVAL_DAYS,
    CONF_SENSOR,
    DOMAIN,
    async_resolve_heater_device,
    smart_eco_state_signal,
)

_LOGGER = logging.getLogger(__name__)
_WINDOW = timedelta(days=7)
_ATTR_MAX_RECORDED_AT = "highest_recorded_at"
_ATTR_SAMPLES_TRACKED = "samples_tracked"

# --- Legionella thermal-conditions model -----------------------------------
#
# This estimates THERMAL CONDITIONS at one sensor. It is not a measurement of
# contamination, and it cannot see the coldest water in the tank. In a 151 L
# electric storage tank with the thermostat at 66 C the measured base was still
# 43.2 C, and the sensor here sits mid-height at best.
#
# The disinfection criteria are deliberately NOT user-lowerable. Guidance that
# prescribes a cycle at all (HSE HSG274 Part 2 cl. 2.25/2.28, ESGLI cl. 3.154)
# asks for the whole vessel at >=60 C for one continuous hour. Below 60 C a
# "cycle" is not a gentler version of the same thing: in water-heater sediment
# 50 C for 4 h produced no measurable inactivation and 55 C left culturable
# cells after 24 h, and after a 4 h/55 C shock with amoebae present populations
# rebounded 5 log10 higher than amoeba-free controls within 4 days. A sub-60 C
# cycle can therefore be worse than none, so it is not offered as an option.
DISINFECTION_TEMP_C = 60.0
DISINFECTION_HOLD = timedelta(minutes=60)

# A hold is hysteretic, not a bare threshold. A mechanical tank thermostat
# cycles: slow decay, fast re-heat. A real 200 L tank observed here rippled
# 59.6-64 C with dips BELOW 60 C lasting 19 and 28 minutes, so a strict
# "continuous hour at or above 60 C" would never complete despite the tank
# sitting at pasteurisation temperature for five hours. Refusing to count that
# is indefensible: at 59.6 C the D-value is 0.51 min against 0.43 min at 60 C.
#
# So the window OPENS at 60 C, stays open while the tank holds above the
# sustain threshold, and closes for good below it. Only time genuinely at or
# above 60 C accumulates toward the hour -- ripple keeps the window open, it
# does not earn credit.
DISINFECTION_SUSTAIN_C = 59.0

# Growth band. The lower bound is the HSE HSG274 Part 2 / ESGLI figure. The
# upper bound is deliberately 50 C rather than their 45 C: 20-45 C is a
# regulatory risk TRIGGER, while measured multiplication does not actually stop
# until 48.4-50.0 C (Kusnetsov 1996, resolved at 0.5 C intervals). Using 45
# would report a tank sitting at 47 C as "out of the growth band" -- which reads
# as safe, when in fact it is still growing and earning no disinfection credit
# either. Both real tanks top out at 48 C, so this is not a hypothetical.
GROWTH_BAND_LOW_C = 20.0
GROWTH_BAND_HIGH_C = 50.0

# Thermal inactivation kinetics from Water Research 2021 (isothermal, 51-61 C):
# D at 55 C = 3.47 min, z = 5.54 C. Credit accrues only inside the validated
# band and is clamped at the top of it, so nothing above 61 C is extrapolated.
KINETICS_D_REF_MIN = 3.47
KINETICS_T_REF_C = 55.0
KINETICS_Z_C = 5.54
KINETICS_MIN_C = 50.0
KINETICS_MAX_C = 61.0

# Longer than this between samples is unobserved time, not evidence of holding
# temperature. The two real tanks sample ~78 s and ~22 min apart respectively.
MAX_CREDITED_GAP = timedelta(minutes=30)

STATE_UNKNOWN_RISK = "Unknown"
STATE_LOW = "Low"
STATE_ELEVATED = "Elevated"
STATE_HIGH = "High"


@dataclass
class MaxTemperatureHistoryStoredData(SensorExtraStoredData):
    """Stored data for the 7-day max temperature sensor."""

    history: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the stored sensor data."""
        return {
            **super().as_dict(),
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> MaxTemperatureHistoryStoredData | None:
        """Initialize stored data from a dict."""
        extra = SensorExtraStoredData.from_dict(restored)
        if extra is None:
            return None

        history = restored.get("history")
        if not isinstance(history, list):
            return None

        cleaned_history: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, dict):
                continue

            timestamp = item.get("timestamp")
            temperature = item.get("temperature")
            if not isinstance(timestamp, str):
                continue

            parsed_timestamp = dt_util.parse_datetime(timestamp)
            if parsed_timestamp is None:
                continue

            try:
                cleaned_temperature = float(temperature)
            except (TypeError, ValueError):
                continue

            cleaned_history.append(
                {
                    "timestamp": parsed_timestamp.isoformat(),
                    "temperature": cleaned_temperature,
                }
            )

        return cls(extra.native_value, extra.native_unit_of_measurement, cleaned_history)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the 7-day max temperature sensor from a config entry."""
    data = {**entry.data, **getattr(entry, "options", {})}
    eco_template = (data.get(CONF_ECO_TEMPLATE) or "").strip() or None

    entities = []

    heater_entity_id = data.get(CONF_HEATER)
    source_sensor_entity_id = data.get(CONF_SENSOR)
    name = data.get(CONF_NAME)

    device_identifiers, device_has_name = async_resolve_heater_device(
        hass, heater_entity_id
    )

    if eco_template is not None:
        entities.append(
            SmartEcoStateSensor(
                hass=hass,
                entry_id=entry.entry_id,
                name=name,
                runtime=hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {}),
                device_identifiers=device_identifiers,
                device_has_name=device_has_name,
            )
        )

    if data.get(CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR, False):
        entities.append(
            MaxTemperatureHistorySensor(
                name=name,
                source_sensor_entity_id=source_sensor_entity_id,
                device_identifier=entry.entry_id,
                device_identifiers=device_identifiers,
                device_has_name=device_has_name,
            )
        )

    if data.get(CONF_ENABLE_LEGIONELLA_SENSOR, False):
        entities.append(
            LegionellaRiskSensor(
                name=name,
                source_sensor_entity_id=source_sensor_entity_id,
                device_identifier=entry.entry_id,
                device_identifiers=device_identifiers,
                device_has_name=device_has_name,
                interval_days=data.get(CONF_LEGIONELLA_INTERVAL_DAYS, 7),
            )
        )

    if entities:
        async_add_entities(entities)


class SmartEcoStateSensor(SensorEntity):
    """Expose Smart Eco policy state."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Smart Eco State"

    def __init__(
        self,
        hass,
        entry_id: str,
        name: str | None,
        runtime: dict,
        device_identifiers,
        device_has_name: bool = False,
    ):
        """Initialize Smart Eco state sensor."""
        self.hass = hass
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_smart_eco_state"

        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        if name and not device_has_name:
            self._attr_name = f"{name} Smart Eco State"
            self._attr_has_entity_name = False

    @property
    def native_value(self):
        """Return Smart Eco state label."""
        return self._runtime.get("smart_eco_state", "Off")

    @property
    def device_info(self):
        """Return device information for device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}

        return {"identifiers": {(DOMAIN, self._entry_id)}}

    async def async_added_to_hass(self) -> None:
        """Subscribe to Smart Eco state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                smart_eco_state_signal(self._entry_id),
                self._async_handle_smart_eco_state_signal,
            )
        )

    def _async_handle_smart_eco_state_signal(self, _payload) -> None:
        """Handle Smart Eco state updates."""
        self.schedule_update_ha_state()


class MaxTemperatureHistorySensor(SensorEntity, RestoreEntity):
    """Track the highest temperature seen in the last 7 days."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_has_entity_name = True
    _attr_name = "Highest Temperature (7 days)"
    _attr_should_poll = False

    def __init__(
        self,
        name: str | None,
        source_sensor_entity_id: str,
        device_identifier: str,
        device_identifiers,
        device_has_name: bool = False,
    ) -> None:
        """Initialize the max temperature history sensor."""
        self._source_sensor_entity_id = source_sensor_entity_id
        self._device_identifier = device_identifier
        self._device_identifiers = device_identifiers
        self._history: list[tuple[datetime, float]] = []
        self._max_recorded_at: datetime | None = None
        self._attr_unique_id = f"{DOMAIN}_{device_identifier}_highest_temperature_7_days"
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = None

        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        if name and not device_has_name:
            self._attr_name = f"{name} Highest Temperature (7 days)"
            self._attr_has_entity_name = False

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}

        return {
            "identifiers": {(DOMAIN, self._device_identifier)},
        }

    @property
    def extra_state_attributes(self):
        """Return extra sensor attributes."""
        attributes = {
            _ATTR_SAMPLES_TRACKED: len(self._history),
        }
        if self._max_recorded_at is not None:
            attributes[_ATTR_MAX_RECORDED_AT] = self._max_recorded_at.isoformat()
        return attributes

    @property
    def extra_restore_state_data(self) -> MaxTemperatureHistoryStoredData:
        """Return sensor-specific restore state data."""
        return MaxTemperatureHistoryStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            [
                {
                    "timestamp": timestamp.isoformat(),
                    "temperature": temperature,
                }
                for timestamp, temperature in self._history
            ],
        )

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to temperature updates."""
        await super().async_added_to_hass()

        if (stored := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = stored.native_value
            self._attr_native_unit_of_measurement = stored.native_unit_of_measurement
            self._history = []
            for item in stored.history:
                parsed = dt_util.parse_datetime(item["timestamp"])
                if parsed is None:
                    continue
                self._history.append((parsed, float(item["temperature"])))
            self._prune_history(dt_util.utcnow())
            self._recalculate_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_sensor_entity_id],
                self._async_source_sensor_changed,
            )
        )

        source_state = self.hass.states.get(self._source_sensor_entity_id)
        if source_state is not None:
            self._async_add_state_sample(source_state.state, source_state.attributes.get("unit_of_measurement"))

        self.async_write_ha_state()

    async def async_get_last_sensor_data(self) -> MaxTemperatureHistoryStoredData | None:
        """Restore stored state and history."""
        if (restored := await self.async_get_last_extra_data()) is None:
            return None
        return MaxTemperatureHistoryStoredData.from_dict(restored.as_dict())

    @callback
    def _async_source_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle source temperature sensor updates."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        self._async_add_state_sample(
            new_state.state,
            new_state.attributes.get("unit_of_measurement"),
            event.time_fired,
        )
        self.async_write_ha_state()

    @callback
    def _async_add_state_sample(
        self,
        state_value: Any,
        unit_of_measurement: str | None,
        when: datetime | None = None,
    ) -> None:
        """Add a numeric source temperature sample to the history window."""
        if state_value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        try:
            temperature = float(state_value)
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring non-numeric temperature state %s", state_value)
            return

        if unit_of_measurement:
            self._attr_native_unit_of_measurement = unit_of_measurement

        timestamp = when or dt_util.utcnow()
        self._history.append((timestamp, temperature))
        self._prune_history(timestamp)
        self._recalculate_state()

    @callback
    def _prune_history(self, reference: datetime) -> None:
        """Keep only samples inside the rolling 7-day window."""
        cutoff = reference - _WINDOW
        self._history = [item for item in self._history if item[0] >= cutoff]

    @callback
    def _recalculate_state(self) -> None:
        """Recalculate the sensor state from the retained history."""
        if not self._history:
            self._attr_native_value = None
            self._max_recorded_at = None
            return

        timestamp, temperature = max(self._history, key=lambda item: item[1])
        self._attr_native_value = temperature
        self._max_recorded_at = timestamp

@dataclass
class LegionellaStoredData(SensorExtraStoredData):
    """Stored data for the Legionella thermal-conditions sensor."""

    history: list[dict[str, Any]]
    last_disinfection_at: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the stored sensor data."""
        return {
            **super().as_dict(),
            "history": self.history,
            "last_disinfection_at": self.last_disinfection_at,
        }

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> LegionellaStoredData | None:
        """Initialize stored data from a dict."""
        extra = SensorExtraStoredData.from_dict(restored)
        if extra is None:
            return None

        history = restored.get("history")
        if not isinstance(history, list):
            history = []

        last = restored.get("last_disinfection_at")
        if not isinstance(last, str):
            last = None

        return cls(
            extra.native_value,
            extra.native_unit_of_measurement,
            history,
            last,
        )


class LegionellaRiskSensor(SensorEntity, RestoreEntity):
    """Report how favourable this tank's temperature history has been to Legionella.

    This is a thermal-conditions index, NOT a probability of contamination. It
    is computed from one sensor at one height, and is blind to the coldest water
    in the tank, to sediment, to biofilm and amoebae, and to every outlet
    downstream. Only a culture or PCR test says what is actually in the water.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_UNKNOWN_RISK, STATE_LOW, STATE_ELEVATED, STATE_HIGH]
    _attr_has_entity_name = True
    _attr_icon = "mdi:virus"
    _attr_name = "Legionella Risk"
    _attr_should_poll = False

    def __init__(
        self,
        name: str | None,
        source_sensor_entity_id: str,
        device_identifier: str,
        device_identifiers,
        interval_days: int,
        device_has_name: bool = False,
    ) -> None:
        """Initialize the Legionella thermal-conditions sensor."""
        self._source_sensor_entity_id = source_sensor_entity_id
        self._device_identifier = device_identifier
        self._device_identifiers = device_identifiers
        self._interval = timedelta(days=max(1, int(interval_days)))
        self._history: list[tuple[datetime, float]] = []
        self._last_disinfection_at: datetime | None = None
        # Progress of a disinfection hold currently in flight. Reset the moment
        # the temperature falls back below the threshold: a partial hold is not
        # banked, because partial treatment is the failure mode this is meant
        # to detect rather than reward.
        self._hold_seconds = 0.0
        self._hold_open = False
        self._attr_native_value = STATE_UNKNOWN_RISK

        # Spell the name out unless the device can supply one. Without this a
        # heater on an unnamed device produced a bare "Legionella Risk", which
        # is ambiguous the moment there is more than one tank.
        if name and not device_has_name:
            self._attr_name = f"{name} Legionella Risk"
            self._attr_has_entity_name = False

        self._attr_unique_id = f"{DOMAIN}_{device_identifier}_legionella_risk"

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}
        return {"identifiers": {(DOMAIN, self._device_identifier)}}

    @property
    def extra_state_attributes(self):
        """Return the numbers behind the state, so it can be argued with."""
        now = dt_util.utcnow()
        window_hours, growth_hours, lethality, peak = self._window_metrics()

        days_since: float | None = None
        if self._last_disinfection_at is not None:
            days_since = round(
                (now - self._last_disinfection_at).total_seconds() / 86400, 2
            )

        return {
            "days_since_disinfection": days_since,
            "last_disinfection_at": (
                self._last_disinfection_at.isoformat()
                if self._last_disinfection_at
                else None
            ),
            "disinfection_interval_days": self._interval.days,
            "disinfection_temperature_c": DISINFECTION_TEMP_C,
            "disinfection_sustain_c": DISINFECTION_SUSTAIN_C,
            "disinfection_hold_minutes": int(DISINFECTION_HOLD.total_seconds() // 60),
            "hold_progress_minutes": round(self._hold_seconds / 60, 1),
            "hold_in_progress": self._hold_open,
            "hours_in_growth_band_7d": round(growth_hours, 1),
            "fraction_in_growth_band_7d": (
                round(growth_hours / window_hours, 3) if window_hours > 0 else None
            ),
            "equivalent_log10_reduction_7d": round(lethality, 2),
            "max_temperature_7d": peak,
            "samples_tracked": len(self._history),
            "caveat": (
                "Thermal conditions at one sensor, not a contamination measurement. "
                "The tank base is colder than this reading and sediment colder still."
            ),
        }

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to temperature updates."""
        await super().async_added_to_hass()

        if (stored := await self._async_get_last_data()) is not None:
            self._last_disinfection_at = (
                dt_util.parse_datetime(stored.last_disinfection_at)
                if stored.last_disinfection_at
                else None
            )
            for item in stored.history:
                parsed = dt_util.parse_datetime(item.get("timestamp", ""))
                if parsed is None:
                    continue
                try:
                    self._history.append((parsed, float(item["temperature"])))
                except (KeyError, TypeError, ValueError):
                    continue
            self._prune(dt_util.utcnow())

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_sensor_entity_id],
                self._async_source_sensor_changed,
            )
        )

        source_state = self.hass.states.get(self._source_sensor_entity_id)
        if source_state is not None:
            self._async_add_sample(source_state.state)

        self._recalculate()
        self.async_write_ha_state()

    @property
    def extra_restore_state_data(self) -> LegionellaStoredData:
        """Return sensor-specific restore state data."""
        return LegionellaStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            [
                {"timestamp": ts.isoformat(), "temperature": temp}
                for ts, temp in self._history
            ],
            self._last_disinfection_at.isoformat()
            if self._last_disinfection_at
            else None,
        )

    async def _async_get_last_data(self) -> LegionellaStoredData | None:
        """Restore stored state, history and last disinfection timestamp."""
        if (restored := await self.async_get_last_extra_data()) is None:
            return None
        return LegionellaStoredData.from_dict(restored.as_dict())

    @callback
    def _async_source_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle source temperature sensor updates."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._async_add_sample(new_state.state, event.time_fired)
        self._recalculate()
        self.async_write_ha_state()

    @callback
    def _async_add_sample(self, state_value: Any, when: datetime | None = None) -> None:
        """Add a temperature sample and advance the disinfection hold."""
        if state_value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            temperature = float(state_value)
        except (TypeError, ValueError):
            return

        timestamp = when or dt_util.utcnow()
        self._advance_hold(timestamp, temperature)
        self._history.append((timestamp, temperature))
        self._prune(timestamp)

    @callback
    def _advance_hold(self, timestamp: datetime, temperature: float) -> None:
        """Advance a hysteretic hold at the disinfection temperature."""
        if temperature < DISINFECTION_SUSTAIN_C:
            # Fell out of the hold entirely. Discard progress rather than
            # banking it: partial treatment is not partial credit.
            if self._hold_open:
                _LOGGER.debug(
                    "%s: disinfection hold abandoned at %.1f minutes (%.1f C)",
                    self.name,
                    self._hold_seconds / 60,
                    temperature,
                )
            self._hold_open = False
            self._hold_seconds = 0.0
            return

        if not self._hold_open:
            if temperature < DISINFECTION_TEMP_C:
                # Warm, but has not yet reached the temperature that opens a
                # hold. Sitting at 59.5 C forever must never qualify.
                return
            self._hold_open = True
            self._hold_seconds = 0.0
            return

        previous = self._history[-1] if self._history else None
        if previous is None:
            return

        elapsed = timestamp - previous[0]
        if elapsed <= timedelta(0) or elapsed > MAX_CREDITED_GAP:
            # Unobserved time is not evidence that temperature was held.
            return

        # Credit the interval at its lower endpoint, so thermostat ripple keeps
        # the window open without earning time it did not spend at temperature.
        if min(previous[1], temperature) < DISINFECTION_TEMP_C:
            return

        self._hold_seconds += elapsed.total_seconds()
        if self._hold_seconds >= DISINFECTION_HOLD.total_seconds():
            _LOGGER.info(
                "%s: disinfection hold completed (>=%.0f C for %.0f minutes at the sensor)",
                self.name,
                DISINFECTION_TEMP_C,
                DISINFECTION_HOLD.total_seconds() / 60,
            )
            self._last_disinfection_at = timestamp
            self._hold_seconds = 0.0
            self._hold_open = False

    @callback
    def _prune(self, reference: datetime) -> None:
        """Keep only samples inside the rolling window."""
        cutoff = reference - _WINDOW
        self._history = [item for item in self._history if item[0] >= cutoff]

    def _window_metrics(self) -> tuple[float, float, float, float | None]:
        """Return (window hours, growth-band hours, log10 reduction, peak C).

        Every interval is credited at the LOWER of its two endpoint
        temperatures, so a brief spike between two widely spaced samples cannot
        claim the whole interval.
        """
        if not self._history:
            return 0.0, 0.0, 0.0, None

        window_seconds = 0.0
        growth_seconds = 0.0
        lethality = 0.0

        for (prev_ts, prev_t), (ts, temp) in zip(self._history, self._history[1:]):
            elapsed = ts - prev_ts
            if elapsed <= timedelta(0) or elapsed > MAX_CREDITED_GAP:
                continue
            seconds = elapsed.total_seconds()
            window_seconds += seconds

            conservative = min(prev_t, temp)
            if GROWTH_BAND_LOW_C <= conservative <= GROWTH_BAND_HIGH_C:
                growth_seconds += seconds
            if conservative >= KINETICS_MIN_C:
                effective = min(conservative, KINETICS_MAX_C)
                d_value = KINETICS_D_REF_MIN * 10 ** (
                    (KINETICS_T_REF_C - effective) / KINETICS_Z_C
                )
                if d_value > 0:
                    lethality += (seconds / 60) / d_value

        peak = max(temp for _, temp in self._history)
        return window_seconds / 3600, growth_seconds / 3600, lethality, peak

    @callback
    def _recalculate(self) -> None:
        """Recalculate the reported risk band."""
        now = dt_util.utcnow()

        if self._last_disinfection_at is None:
            # Never seen a qualifying cycle. Stay Unknown until enough history
            # has accumulated to make the silence meaningful.
            observed = (
                (now - self._history[0][0]) if self._history else timedelta(0)
            )
            if observed < self._interval:
                self._attr_native_value = STATE_UNKNOWN_RISK
            elif observed < 2 * self._interval:
                self._attr_native_value = STATE_ELEVATED
            else:
                self._attr_native_value = STATE_HIGH
            return

        since = now - self._last_disinfection_at
        if since <= self._interval:
            self._attr_native_value = STATE_LOW
        elif since <= 2 * self._interval:
            self._attr_native_value = STATE_ELEVATED
        else:
            self._attr_native_value = STATE_HIGH
