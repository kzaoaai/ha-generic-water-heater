"""Sensor platform for Generic Water Heater."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_NAME,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from . import (
    CONF_ENABLE_ENERGY_SPLIT,
    CONF_ENABLE_HEAT_UP_RATE,
    CONF_SURPLUS_ENTITY,
    CONF_SURPLUS_STATE,
    CONF_UTILITY_ENTITY,
    CONF_UTILITY_STATE,
    DEFAULT_ACTIVE_STATE,
    remove_switched_off_entities,
    name_heater_entity,
    eco_condition_configured,
    CONF_POWER_SENSOR,
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_HEATER,
    CONF_LEGIONELLA_INTERVAL_DAYS,
    CONF_SENSOR,
    DOMAIN,
    async_resolve_heater_device,
    legionella_risk_signal,
    smart_eco_state_signal,
)
from .temperature_filter import finite
from .temperature_tracking import async_track_filtered_temperature
from .element_health import NO_DRAW_W, to_watts
from .energy_split import SOURCES
from .insights import (
    EnergySplitTracker,
    EnergyTodaySensor,
    HeatUpRateSensor,
    ReadyAtSensor,
    energy_split_unique_ids,
    heat_rate_unique_ids,
)

_LOGGER = logging.getLogger(__name__)
_WINDOW = timedelta(days=7)
_ATTR_MAX_RECORDED_AT = "highest_recorded_at"
_ATTR_SAMPLES_TRACKED = "samples_tracked"

# History is kept in 5-minute buckets (4.6.1), not one entry per reading. A
# sensor reporting every ~78 s kept ~7.7k samples per 7-day window, stored in
# full by both history sensors in core.restore_state (~600 KB each, rewritten
# every 15 minutes), and `samples_tracked` changed on every reading, so each
# reading wrote a recorder row even when the state had not moved. It now counts
# buckets. The disinfection hold never reads this history -- it follows each
# live reading against the previous one -- so only the 7-day figures and the
# maximum see the coarser grain.
HISTORY_BUCKET = timedelta(minutes=5)
_BUCKET_MINUTES = int(HISTORY_BUCKET.total_seconds() // 60)


@dataclass
class _Bucket:
    """The readings of one HISTORY_BUCKET: lowest, highest and when it peaked."""

    start: datetime
    t_min: float
    t_max: float
    max_at: datetime

    def as_stored(self) -> list[float]:
        """Compact on purpose: [start epoch s, min, max, peak offset s].

        Two of these per tank per 5 minutes for 7 days; as dicts with ISO
        strings the restore data was still ~200 KB per sensor.
        """
        return [
            int(self.start.timestamp()),
            self.t_min,
            self.t_max,
            round((self.max_at - self.start).total_seconds(), 1),
        ]


def _bucket_start(timestamp: datetime) -> datetime:
    return timestamp - timedelta(
        minutes=timestamp.minute % _BUCKET_MINUTES,
        seconds=timestamp.second,
        microseconds=timestamp.microsecond,
    )


def _add_to_buckets(buckets: list[_Bucket], timestamp: datetime, temperature: float) -> None:
    """Fold one reading into the bucket list, which stays ordered by start."""
    start = _bucket_start(timestamp)
    for bucket in reversed(buckets):
        if bucket.start == start:
            bucket.t_min = min(bucket.t_min, temperature)
            if temperature > bucket.t_max:
                bucket.t_max = temperature
                bucket.max_at = timestamp
            return
        if bucket.start < start:
            break
    buckets.append(_Bucket(start, temperature, temperature, timestamp))
    if len(buckets) > 1 and buckets[-2].start > start:
        buckets.sort(key=lambda bucket: bucket.start)


def _prune_buckets(buckets: list[_Bucket], reference: datetime) -> list[_Bucket]:
    """Keep the buckets that overlap the rolling 7-day window."""
    cutoff = reference - _WINDOW
    return [bucket for bucket in buckets if bucket.start + HISTORY_BUCKET > cutoff]


def _buckets_from_stored(items: list[Any]) -> list[_Bucket]:
    """Read stored history: buckets, or raw samples stored before 4.6.1."""
    buckets: list[_Bucket] = []
    for item in items:
        try:
            if isinstance(item, list):
                start = dt_util.utc_from_timestamp(float(item[0]))
                buckets.append(
                    _Bucket(
                        start,
                        float(item[1]),
                        float(item[2]),
                        start + timedelta(seconds=float(item[3])),
                    )
                )
            elif isinstance(item, dict):
                timestamp = dt_util.parse_datetime(str(item["timestamp"]))
                if timestamp is None:
                    continue
                _add_to_buckets(buckets, timestamp, float(item["temperature"]))
        except (IndexError, KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    buckets.sort(key=lambda bucket: bucket.start)
    return buckets


def _last_raw_sample(items: list[Any]) -> tuple[datetime, float] | None:
    """The newest raw sample in history stored before 4.6.1, if any."""
    latest: tuple[datetime, float] | None = None
    for item in items:
        if not isinstance(item, dict) or "timestamp" not in item:
            continue
        try:
            timestamp = dt_util.parse_datetime(str(item["timestamp"]))
            temperature = float(item["temperature"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp is not None and (latest is None or timestamp > latest[0]):
            latest = (timestamp, temperature)
    return latest

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

# A single sample below the sustain threshold is not proof the tank left
# pasteurisation temperature. The sensor reads one point on a stratified
# vessel, so a brief draw-off or a stratification artefact can dip it while the
# bulk is still hot -- and a real 200 L trace came within 0.6 C of discarding a
# five-hour hold on exactly such a dip. So one sub-sustain sample is forgiven if
# the very next sample recovers, and only if that recovery arrives within the
# normal sampling cadence: unobserved time is still not evidence.
SUSTAIN_BREACH_GRACE = MAX_CREDITED_GAP

# The one exception to MAX_CREDITED_GAP (4.6.0): a gap longer than it is
# credited toward a hold when the element stayed OFF for the whole gap and the
# readings at both ends are at or above the disinfection temperature. With no
# heat source the tank can only cool, so it cannot have dipped below 60 C and
# come back. Needed because a sensor that reports only on a ~0.5 C change goes
# silent for more than half an hour while a tank sits idle above 60 C, and the
# hold stalled there (seen 2026-10-23: 10.8 min banked, then 34+ min of
# silence, nothing credited). Bounded, because convection after a draw can lift
# one point of a stratified tank a little with no heat input, and the bound is
# what limits how much time that can wrongly claim.
MAX_ELEMENT_OFF_GAP = timedelta(hours=2)

# Below this, a dip is a real event rather than a bad reading, and closes the
# hold immediately with no grace. One degree under the sustain threshold is
# already about double the ripple amplitude the real tanks show (a 59.6-64.2 C
# mechanical band dips ~0.4 C under 60), so forgiveness stays scoped to a single
# implausible sample and never covers a tank that genuinely lost its heat.
# Erring the other way would credit an hour the tank never held, and this model
# refuses credit when in doubt.
SUSTAIN_HARD_FLOOR_C = 58.0

STATE_UNKNOWN_RISK = "Unknown"
STATE_LOW = "Low"
STATE_ELEVATED = "Elevated"
STATE_HIGH = "High"


def _element_off_since(
    hass, heater_entity_id: str | None, power_sensor_entity_id: str | None = None
) -> Callable[[datetime], bool]:
    """Return a check: has the element been off since a given time?

    Two independent pieces of evidence, either of which is enough:

    - The heater switch: off now, and last changed no later than the time
      asked about.
    - The configured meter (4.6.4): reading below ``NO_DRAW_W`` now, and last
      changed no later than the time asked about. This is the case the switch
      cannot see: PERFORMANCE and a disinfection cycle run the tank to its own
      mechanical thermostat on purpose, so the relay stays closed while the
      thermostat has opened the circuit and the tank coasts. Seen 2026-11-14:
      54.2 min banked, the thermostat cut out at 64.7 C with the relay on, the
      sensor then went silent for 37 min at 64+ C, and the hold stalled.
      Read only as evidence for a hold, never for control.

    Anything else reads False - unavailable, unknown or non-numeric is not off,
    and any change inside the gap (on and back off, a dropout and back, a
    meter reporting a draw) moves last_changed past it. A restart resets
    last_changed, so a gap across one is never credited this way.
    """

    def _unchanged_since(entity_id: str | None, since: datetime, is_off) -> bool:
        if not entity_id:
            return False
        state = hass.states.get(entity_id)
        return state is not None and state.last_changed <= since and is_off(state)

    def _meter_reads_no_draw(state) -> bool:
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return False
        unit = state.attributes.get("unit_of_measurement")
        return 0.0 <= to_watts(value, unit) < NO_DRAW_W

    def check(since: datetime) -> bool:
        return _unchanged_since(
            heater_entity_id, since, lambda state: state.state == STATE_OFF
        ) or _unchanged_since(power_sensor_entity_id, since, _meter_reads_no_draw)

    return check


@dataclass
class MaxTemperatureHistoryStoredData(SensorExtraStoredData):
    """Stored data for the 7-day max temperature sensor."""

    history: list[Any]

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

        cleaned_history: list[Any] = []
        for item in history:
            if isinstance(item, list):
                # A 4.6.1 bucket; validated when it is read (_buckets_from_stored).
                cleaned_history.append(item)
                continue
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

    entities = []

    heater_entity_id = data.get(CONF_HEATER)
    source_sensor_entity_id = data.get(CONF_SENSOR)
    name = data.get(CONF_NAME)

    device_identifiers, device_has_name = async_resolve_heater_device(
        hass, heater_entity_id, data.get(CONF_POWER_SENSOR)
    )

    if eco_condition_configured(data):
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
                runtime=hass.data.setdefault(DOMAIN, {}).setdefault(
                    entry.entry_id, {}
                ),
                element_off_since=_element_off_since(
                    hass,
                    heater_entity_id,
                    (data.get(CONF_POWER_SENSOR) or "").strip() or None,
                ),
            )
        )
        for detail in (
            DaysUntilDisinfectionSensor,
            DisinfectionDueSensor,
            DisinfectionHoldProgressSensor,
        ):
            entities.append(
                detail(
                    name=name,
                    entry_id=entry.entry_id,
                    runtime=hass.data[DOMAIN][entry.entry_id],
                    device_identifiers=device_identifiers,
                    device_has_name=device_has_name,
                )
            )

    power_sensor = (data.get(CONF_POWER_SENSOR) or "").strip() or None
    meter = power_sensor
    if meter is not None:
        source = er.async_get(hass).async_get(meter)
        if source is not None and source.platform == DOMAIN:
            meter = None
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})

    if data.get(CONF_ENABLE_HEAT_UP_RATE, True) and heater_entity_id and source_sensor_entity_id:
        entities.append(
            HeatUpRateSensor(
                hass, entry.entry_id, name, heater_entity_id, source_sensor_entity_id,
                meter, runtime, device_identifiers, device_has_name,
            )
        )
        entities.append(
            ReadyAtSensor(hass, entry.entry_id, name, runtime, device_identifiers, device_has_name)
        )
    else:
        remove_switched_off_entities(hass, entry.entry_id, "sensor", heat_rate_unique_ids(entry.entry_id))

    if data.get(CONF_ENABLE_ENERGY_SPLIT, True) and meter is not None:
        tracker = EnergySplitTracker(
            hass,
            meter,
            (
                (data.get(CONF_SURPLUS_ENTITY) or "").strip() or None,
                data.get(CONF_SURPLUS_STATE) or DEFAULT_ACTIVE_STATE,
            ),
            (
                (data.get(CONF_UTILITY_ENTITY) or "").strip() or None,
                data.get(CONF_UTILITY_STATE) or DEFAULT_ACTIVE_STATE,
            ),
        )
        for split_source in SOURCES:
            entities.append(
                EnergyTodaySensor(
                    hass, entry.entry_id, name, split_source, tracker,
                    device_identifiers, device_has_name,
                )
            )
    elif not data.get(CONF_ENABLE_ENERGY_SPLIT, True):
        remove_switched_off_entities(hass, entry.entry_id, "sensor", energy_split_unique_ids(entry.entry_id))

    if power_sensor is not None:
        source = er.async_get(hass).async_get(power_sensor)
        # Never mirror a mirror (config_flow._power_sensor_errors refuses one
        # on submit; this covers an entry written another way).
        if source is None or source.platform != DOMAIN:
            entities.append(
                ElementPowerSensor(
                    hass=hass,
                    entry_id=entry.entry_id,
                    name=name,
                    source_entity_id=power_sensor,
                    device_identifiers=device_identifiers,
                    device_has_name=device_has_name,
                )
            )

    if entities:
        async_add_entities(entities)


class SmartEcoStateSensor(SensorEntity):
    """Expose Smart Eco policy state."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Smart Eco State"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        name_heater_entity(self, "sensor", name, "Smart Eco State", device_has_name)

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
    _attr_entity_category = EntityCategory.DIAGNOSTIC
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
        self._history: list[_Bucket] = []
        self._max_recorded_at: datetime | None = None
        self._attr_unique_id = f"{DOMAIN}_{device_identifier}_highest_temperature_7_days"
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = None

        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        name_heater_entity(self, "sensor", name, "Highest Temperature (7 days)", device_has_name)

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
            [bucket.as_stored() for bucket in self._history],
        )

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to temperature updates."""
        await super().async_added_to_hass()

        if (stored := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = stored.native_value
            self._attr_native_unit_of_measurement = stored.native_unit_of_measurement
            self._history = _buckets_from_stored(stored.history)
            self._prune_history(dt_util.utcnow())
            self._recalculate_state()

        self.async_on_remove(
            async_track_filtered_temperature(
                self.hass,
                self._source_sensor_entity_id,
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
    def _async_source_sensor_changed(
        self, value: Any, when: datetime, attributes: dict[str, Any]
    ) -> None:
        """Handle a temperature reading that survived the spike filter."""
        if value is None:
            return

        self._async_add_state_sample(
            value,
            attributes.get("unit_of_measurement"),
            when,
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

        # Finite only: the startup read does not come through the tracking
        # helper, and a "nan" there made the weekly maximum NaN.
        temperature = finite(state_value)
        if temperature is None:
            _LOGGER.debug("Ignoring non-numeric temperature state %s", state_value)
            return

        if unit_of_measurement:
            self._attr_native_unit_of_measurement = unit_of_measurement

        timestamp = when or dt_util.utcnow()
        _add_to_buckets(self._history, timestamp, temperature)
        self._prune_history(timestamp)
        self._recalculate_state()

    @callback
    def _prune_history(self, reference: datetime) -> None:
        """Keep only buckets inside the rolling 7-day window."""
        self._history = _prune_buckets(self._history, reference)

    @callback
    def _recalculate_state(self) -> None:
        """Recalculate the sensor state from the retained history."""
        if not self._history:
            self._attr_native_value = None
            self._max_recorded_at = None
            return

        peak = max(self._history, key=lambda bucket: bucket.t_max)
        self._attr_native_value = peak.t_max
        self._max_recorded_at = peak.max_at

@dataclass
class LegionellaStoredData(SensorExtraStoredData):
    """Stored data for the Legionella thermal-conditions sensor."""

    history: list[Any]
    last_disinfection_at: str | None
    hold_seconds: float
    hold_open: bool
    # When this sensor first saw the tank. The history is pruned to 7 days, so
    # it cannot answer "how long have we been watching" for a tank that has
    # never been disinfected (see _recalculate).
    first_observed_at: str | None = None
    # The last reading, which the hold advances from. Kept apart from the
    # bucketed history, which does not keep individual readings.
    last_sample: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the stored sensor data."""
        return {
            **super().as_dict(),
            "history": self.history,
            "last_disinfection_at": self.last_disinfection_at,
            "hold_seconds": self.hold_seconds,
            "hold_open": self.hold_open,
            "first_observed_at": self.first_observed_at,
            "last_sample": self.last_sample,
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

        try:
            hold_seconds = float(restored.get("hold_seconds", 0.0))
        except (TypeError, ValueError):
            hold_seconds = 0.0
        if hold_seconds < 0:
            hold_seconds = 0.0

        hold_open = restored.get("hold_open") is True

        first = restored.get("first_observed_at")
        if not isinstance(first, str):
            first = None

        last_sample = restored.get("last_sample")
        if not isinstance(last_sample, dict):
            last_sample = None

        return cls(
            extra.native_value,
            extra.native_unit_of_measurement,
            history,
            last,
            hold_seconds,
            hold_open,
            first,
            last_sample,
        )


def _stored_last_sample(stored: LegionellaStoredData) -> tuple[datetime, float] | None:
    """The last reading from stored data, in either format."""
    item = stored.last_sample
    if item is not None:
        try:
            timestamp = dt_util.parse_datetime(str(item["timestamp"]))
            if timestamp is not None:
                return timestamp, float(item["temperature"])
        except (KeyError, TypeError, ValueError):
            pass
    return _last_raw_sample(stored.history)


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
        runtime: dict | None = None,
        element_off_since: Callable[[datetime], bool] | None = None,
    ) -> None:
        """Initialize the Legionella thermal-conditions sensor."""
        self._runtime = runtime
        # Whether the heater's element has been off, continuously, since a
        # given time (MAX_ELEMENT_OFF_GAP). None: never, so no long gap counts.
        self._element_off_since = element_off_since
        self._source_sensor_entity_id = source_sensor_entity_id
        self._device_identifier = device_identifier
        self._device_identifiers = device_identifiers
        self._interval = timedelta(days=max(1, int(interval_days)))
        self._history: list[_Bucket] = []
        self._last_sample: tuple[datetime, float] | None = None
        # (signature of the closed buckets, (window h, growth h, lethality)).
        self._metrics_cache: tuple[Any, tuple[float, float, float]] | None = None
        # What _publish_risk last dispatched (see there).
        self._last_published: Any = None
        self._last_disinfection_at: datetime | None = None
        self._first_observed_at: datetime | None = None
        # Progress of a disinfection hold currently in flight. Reset the moment
        # the temperature falls back below the threshold: a partial hold is not
        # banked, because partial treatment is the failure mode this is meant
        # to detect rather than reward.
        self._hold_seconds = 0.0
        self._hold_open = False
        # Timestamp of a single sub-sustain reading awaiting confirmation. Not
        # persisted: a restart lands with it cleared, which can at worst forgive
        # one extra dip on a hold that the old code discarded outright.
        self._sustain_breach_at: datetime | None = None
        self._attr_native_value = STATE_UNKNOWN_RISK

        # Spell the name out unless the device can supply one. Without this a
        # heater on an unnamed device produced a bare "Legionella Risk", which
        # is ambiguous the moment there is more than one tank.
        name_heater_entity(self, "sensor", name, "Legionella Risk", device_has_name)

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
        window_hours, growth_hours, lethality, peak = self._window_metrics()

        # Days until the next cycle is due and the live hold progress used to be
        # attributes here; since 3.0.0 they are entities of their own, so they
        # have history and can drive automations without a template.
        return {
            "last_disinfection_at": (
                self._last_disinfection_at.isoformat()
                if self._last_disinfection_at
                else None
            ),
            "disinfection_interval_days": self._interval.days,
            "disinfection_temperature_c": DISINFECTION_TEMP_C,
            "disinfection_sustain_c": DISINFECTION_SUSTAIN_C,
            "disinfection_hold_minutes": int(DISINFECTION_HOLD.total_seconds() // 60),
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
            self._history = _buckets_from_stored(stored.history)
            self._last_sample = _stored_last_sample(stored)
            self._first_observed_at = (
                dt_util.parse_datetime(stored.first_observed_at)
                if stored.first_observed_at
                else None
            )
            if self._first_observed_at is None and self._history:
                # Stored before 4.6.1: the oldest sample is the best we know.
                self._first_observed_at = self._history[0].start
            self._restore_hold(stored, dt_util.utcnow())
            self._prune(dt_util.utcnow())

        self.async_on_remove(
            async_track_filtered_temperature(
                self.hass,
                self._source_sensor_entity_id,
                self._async_source_sensor_changed,
            )
        )

        source_state = self.hass.states.get(self._source_sensor_entity_id)
        if source_state is not None:
            self._async_add_sample(source_state.state)

        self._recalculate()
        self._publish_risk()
        self.async_write_ha_state()

    @callback
    def _publish_risk(self) -> None:
        """Share the verdict with the water heater on the same config entry.

        The two are separate platforms with no reference to each other, so the
        disinfection policy reads the risk through the entry's runtime dict and
        is woken by a dispatcher signal -- the same wiring the Smart Eco select
        already uses in the other direction.
        """
        progress = round(self._hold_seconds / 60, 1)
        if self._runtime is not None:
            self._runtime["legionella_risk"] = self._attr_native_value
            self._runtime["legionella_hold_progress_minutes"] = progress
            self._runtime["legionella_last_disinfection_at"] = self._last_disinfection_at
            self._runtime["legionella_interval_days"] = self._interval.days

        if getattr(self, "hass", None) is None:
            return
        # Only when something a listener reads has changed. This used to fire
        # on every reading: the heater re-evaluated its disinfection policy and
        # the detail sensors rewrote their state each time, for a verdict that
        # changes a few times a week.
        published = (
            self._attr_native_value,
            self._last_disinfection_at,
            progress,
            self._interval.days,
        )
        if published == self._last_published:
            return
        self._last_published = published
        async_dispatcher_send(
            self.hass,
            legionella_risk_signal(self._device_identifier),
            self._attr_native_value,
        )

    @callback
    def _restore_hold(self, stored: LegionellaStoredData, now: datetime) -> None:
        """Resume a hold that was in flight, but only if it stayed observed.

        Most of a real hold is banked with the element already off, coasting
        down from the thermostat cutout -- a measured 200 L tank credited its
        final minutes seven minutes after the switch turned off. Discarding that
        progress on every restart silently threw away nearly complete hours.

        A hold is only resumed when the gap across the restart is inside
        MAX_CREDITED_GAP. Longer than that and there is no evidence the tank
        stayed above the sustain threshold while Home Assistant was down, and
        unobserved time must not be mistaken for held temperature.
        """
        if not stored.hold_open or stored.hold_seconds <= 0:
            return

        if self._last_sample is None:
            self._last_sample = _stored_last_sample(stored)
        if self._last_sample is None:
            return

        gap = now - self._last_sample[0]
        if gap > MAX_CREDITED_GAP or gap < timedelta(0):
            _LOGGER.debug(
                "%s: not resuming disinfection hold, %.1f minutes unobserved",
                self.name,
                gap.total_seconds() / 60,
            )
            return

        self._hold_open = True
        self._hold_seconds = stored.hold_seconds
        _LOGGER.debug(
            "%s: resumed disinfection hold at %.1f minutes",
            self.name,
            self._hold_seconds / 60,
        )

    @property
    def extra_restore_state_data(self) -> LegionellaStoredData:
        """Return sensor-specific restore state data."""
        return LegionellaStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            [bucket.as_stored() for bucket in self._history],
            self._last_disinfection_at.isoformat()
            if self._last_disinfection_at
            else None,
            self._hold_seconds,
            self._hold_open,
            self._first_observed_at.isoformat() if self._first_observed_at else None,
            (
                {
                    "timestamp": self._last_sample[0].isoformat(),
                    "temperature": self._last_sample[1],
                }
                if self._last_sample is not None
                else None
            ),
        )

    async def _async_get_last_data(self) -> LegionellaStoredData | None:
        """Restore stored state, history and last disinfection timestamp."""
        if (restored := await self.async_get_last_extra_data()) is None:
            return None
        return LegionellaStoredData.from_dict(restored.as_dict())

    @callback
    def _async_source_sensor_changed(
        self, value: Any, when: datetime, _attributes: dict[str, Any]
    ) -> None:
        """Handle a temperature reading that survived the spike filter."""
        if value is None:
            return
        self._async_add_sample(value, when)
        self._recalculate()
        self._publish_risk()
        self.async_write_ha_state()

    @callback
    def _async_add_sample(self, state_value: Any, when: datetime | None = None) -> None:
        """Add a temperature sample and advance the disinfection hold."""
        if state_value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        # Finite only: the startup read does not come through the tracking
        # helper, and a "nan" there opened a disinfection hold (it is "not
        # below" every threshold, because every comparison with it is false).
        temperature = finite(state_value)
        if temperature is None:
            return

        timestamp = when or dt_util.utcnow()
        if self._first_observed_at is None:
            self._first_observed_at = timestamp
        self._advance_hold(timestamp, temperature)
        self._last_sample = (timestamp, temperature)
        _add_to_buckets(self._history, timestamp, temperature)
        self._prune(timestamp)

    def _element_off_throughout(self, since: datetime, elapsed: timedelta) -> bool:
        """Whether a long gap may be credited: element off for all of it."""
        return (
            elapsed <= MAX_ELEMENT_OFF_GAP
            and self._element_off_since is not None
            and self._element_off_since(since)
        )

    @callback
    def _advance_hold(self, timestamp: datetime, temperature: float) -> None:
        """Advance a hysteretic hold at the disinfection temperature."""
        if temperature < DISINFECTION_SUSTAIN_C:
            self._handle_sustain_breach(timestamp, temperature)
            return

        if self._sustain_breach_at is not None:
            # Recovered. Forgive the dip only if it was brief enough to read as
            # ripple rather than as the tank genuinely leaving temperature.
            late = timestamp - self._sustain_breach_at > SUSTAIN_BREACH_GRACE
            self._sustain_breach_at = None
            if late and self._hold_open:
                _LOGGER.debug(
                    "%s: disinfection hold abandoned at %.1f minutes "
                    "(recovered too late to call the dip transient)",
                    self.name,
                    self._hold_seconds / 60,
                )
                self._hold_open = False
                self._hold_seconds = 0.0

        if not self._hold_open:
            if temperature < DISINFECTION_TEMP_C:
                # Warm, but has not yet reached the temperature that opens a
                # hold. Sitting at 59.5 C forever must never qualify.
                return
            self._hold_open = True
            self._hold_seconds = 0.0
            return

        previous = self._last_sample
        if previous is None:
            return

        elapsed = timestamp - previous[0]
        if elapsed <= timedelta(0):
            return
        if elapsed > MAX_CREDITED_GAP and not self._element_off_throughout(
            previous[0], elapsed
        ):
            # Unobserved time is not evidence that temperature was held.
            return

        # Credit the interval at its lower endpoint, so thermostat ripple keeps
        # the window open without earning time it did not spend at temperature.
        # For a long element-off gap this is also the "both ends at or above
        # 60 C" half of MAX_ELEMENT_OFF_GAP.
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
            self._sustain_breach_at = None

    @callback
    def _handle_sustain_breach(self, timestamp: datetime, temperature: float) -> None:
        """Handle one sample below the sustain threshold.

        Discarding a hold is deliberate -- partial treatment is not partial
        credit -- but it must take more than a single reading to do it, because
        one sensor on a stratified tank dips for reasons the bulk water does
        not share.
        """
        if not self._hold_open:
            self._sustain_breach_at = None
            return

        if temperature >= SUSTAIN_HARD_FLOOR_C and self._sustain_breach_at is None:
            self._sustain_breach_at = timestamp
            _LOGGER.debug(
                "%s: disinfection hold dipped to %.1f C at %.1f minutes, "
                "awaiting the next sample before discarding it",
                self.name,
                temperature,
                self._hold_seconds / 60,
            )
            return

        _LOGGER.debug(
            "%s: disinfection hold abandoned at %.1f minutes (%.1f C)",
            self.name,
            self._hold_seconds / 60,
            temperature,
        )
        self._hold_open = False
        self._hold_seconds = 0.0
        self._sustain_breach_at = None

    @callback
    def _prune(self, reference: datetime) -> None:
        """Keep only buckets inside the rolling window."""
        self._history = _prune_buckets(self._history, reference)

    def _window_metrics(self) -> tuple[float, float, float, float | None]:
        """Return (window hours, growth-band hours, log10 reduction, peak C).

        Every interval between consecutive buckets is credited at the LOWER of
        the two buckets' lowest readings, so a brief spike between two widely
        spaced samples cannot claim the whole interval. The bucket still being
        filled is left out of the sums -- its lowest reading moves with every
        cooling sample, which would rewrite the attributes on every reading --
        and the sums are cached until a bucket closes or ages out. The peak
        includes it.
        """
        if not self._history:
            return 0.0, 0.0, 0.0, None

        closed = self._history
        if self._history[-1].start >= _bucket_start(dt_util.utcnow()):
            closed = self._history[:-1]
        signature = (
            len(closed),
            closed[0].start if closed else None,
            closed[-1].start if closed else None,
        )
        if self._metrics_cache is None or self._metrics_cache[0] != signature:
            self._metrics_cache = (signature, self._sum_window(closed))
        window_hours, growth_hours, lethality = self._metrics_cache[1]
        peak = max(bucket.t_max for bucket in self._history)
        return window_hours, growth_hours, lethality, peak

    @staticmethod
    def _sum_window(buckets: list[_Bucket]) -> tuple[float, float, float]:
        window_seconds = 0.0
        growth_seconds = 0.0
        lethality = 0.0

        for prev, bucket in zip(buckets, buckets[1:]):
            prev_t, temp = prev.t_min, bucket.t_min
            elapsed = bucket.start - prev.start
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

        return window_seconds / 3600, growth_seconds / 3600, lethality

    @callback
    def _recalculate(self) -> None:
        """Recalculate the reported risk band."""
        now = dt_util.utcnow()

        if self._last_disinfection_at is None:
            # Never seen a qualifying cycle. Stay Unknown until enough history
            # has accumulated to make the silence meaningful. Measured from the
            # first sample this sensor ever saw, not the oldest one retained:
            # the history is pruned to 7 days, so with an interval of 7 days or
            # more the verdict could never leave Unknown, and "As needed" never
            # started the first cycle on the tank that needed it most.
            observed = (
                (now - self._first_observed_at)
                if self._first_observed_at is not None
                else timedelta(0)
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


class _LegionellaDetailSensor(SensorEntity):
    """A number the Legionella Risk sensor computes, exposed as its own entity.

    Reads what the risk sensor publishes to the entry's runtime dict and wakes
    on its dispatcher signal, the same wiring the water heater already uses to
    read the verdict. Nothing is computed twice.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Explains the heater's behaviour rather than controls it, so it lives in
    # the device page's Diagnostic section; the Legionella Risk verdict and Hot
    # Water In Use stay primary.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _label: str
    _key: str

    def __init__(
        self,
        name: str | None,
        entry_id: str,
        runtime: dict,
        device_identifiers,
        device_has_name: bool = False,
    ) -> None:
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_name = self._label
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{self._key}"
        name_heater_entity(self, "sensor", name, self._label, device_has_name)

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}
        return {"identifiers": {(DOMAIN, self._entry_id)}}

    async def async_added_to_hass(self) -> None:
        """Follow the risk sensor's updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                legionella_risk_signal(self._entry_id),
                self._async_risk_updated,
            )
        )

    @callback
    def _async_risk_updated(self, _risk) -> None:
        self.async_write_ha_state()


class DaysUntilDisinfectionSensor(_LegionellaDetailSensor):
    """Days until the disinfection interval lapses; negative once overdue.

    Unknown until a cycle has been seen: with no last cycle there is no date
    to count towards, and inventing one would read as a schedule.
    """

    # Named to sort beside Disinfection Hold Progress; the unique_id key keeps
    # its old name so existing installs keep their entity_id.
    _label = "Disinfection Due In"
    _key = "legionella_days_until_disinfection"
    # No statistics (4.6.1): a countdown changes only because the clock moved,
    # so its long-term statistics recorded nothing but the passage of time.
    # Disinfection Due is the same fact as a deadline, which a frontend counts
    # down by itself; this entity stays so nothing reading it breaks.
    _attr_state_class = None
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:calendar-clock"

    async def async_added_to_hass(self) -> None:
        """Also tick on a clock: a flat tank may not report for twenty minutes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(minutes=10)
            )
        )

    @callback
    def _async_tick(self, _now) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        last = self._runtime.get("legionella_last_disinfection_at")
        interval = self._runtime.get("legionella_interval_days")
        if last is None or interval is None:
            return None
        due = last + timedelta(days=interval)
        return round((due - dt_util.utcnow()).total_seconds() / 86400, 2)


class DisinfectionDueSensor(_LegionellaDetailSensor):
    """When the disinfection interval lapses: a deadline, not time remaining.

    Changes only when a hold completes (or the interval is reconfigured), so it
    writes nothing while it waits; a dashboard counts down to it by itself.
    Unknown until a cycle has been seen, like Disinfection Due In.
    """

    _label = "Disinfection Due"
    _key = "legionella_disinfection_due"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_state_class = None
    _attr_icon = "mdi:calendar-check"

    @property
    def native_value(self) -> datetime | None:
        last = self._runtime.get("legionella_last_disinfection_at")
        interval = self._runtime.get("legionella_interval_days")
        if last is None or interval is None:
            return None
        return last + timedelta(days=interval)


class DisinfectionHoldProgressSensor(_LegionellaDetailSensor):
    """Minutes banked toward the current hold at the disinfection temperature."""

    _label = "Disinfection Hold Progress"
    _key = "legionella_hold_progress"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:timer-sand"

    @property
    def native_value(self) -> float | None:
        return self._runtime.get("legionella_hold_progress_minutes")


# The mirror skips updates smaller than this. Meters report every few watts of
# standby noise and voltage drift, and each report was a state write and a
# recorder row on a sensor nothing reads for control. An element switching on
# or off moves it by a kilowatt or more.
MIRROR_DEADBAND_W = 5.0


def _within_deadband(shown, mirror) -> bool:
    available, value, unit = shown
    if not (available and mirror._attr_available) or unit != mirror._attr_native_unit_of_measurement:
        return False
    if value is None or mirror._attr_native_value is None:
        return False
    return abs(
        to_watts(mirror._attr_native_value, unit) - to_watts(value, unit)
    ) < MIRROR_DEADBAND_W


class ElementPowerSensor(SensorEntity):
    """The element's draw, mirrored from its configured meter onto this device.

    For the UI only: the heater's device page shows what it draws beside its
    temperature and state. The meter stays the source of truth - it is what the
    `power_sensor` attribute names, and what any consumer reads.

    It must never be counted twice:
    - NO state_class: no long-term statistics, so the Energy dashboard cannot
      offer it and nothing sums it into energy. (The meter carries those.)
    - Named "Power (mirror)", so its entity id ends `_power_mirror`, never
      `_power`: a template summing every `..._power` sensor in watts exists on
      the author's install (dormant, 2026-10-08) and would otherwise count
      the element twice. `mirror_of` names the meter for anyone configuring a
      load balancer or a template by hand.
    - It is never created from one of this integration's own entities, and the
      config flow refuses one as `power_sensor`.
    Unavailable whenever the meter is unavailable, missing or non-numeric.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Power (mirror)"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_icon = "mdi:flash"

    def __init__(
        self,
        hass,
        entry_id: str,
        name: str | None,
        source_entity_id: str,
        device_identifiers,
        device_has_name: bool = False,
    ) -> None:
        """Initialise the mirror."""
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_power_mirror"
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = None
        self._attr_available = False
        self._attr_extra_state_attributes = {"mirror_of": source_entity_id}
        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        name_heater_entity(self, "sensor", name, "Power (mirror)", device_has_name)

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}
        return {"identifiers": {(DOMAIN, self._entry_id)}}

    async def async_added_to_hass(self) -> None:
        """Follow the meter."""
        await super().async_added_to_hass()
        self._mirror(self.hass.states.get(self._source))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source], self._async_source_changed
            )
        )

    @callback
    def _async_source_changed(self, event) -> None:
        shown = (
            self._attr_available,
            self._attr_native_value,
            self._attr_native_unit_of_measurement,
        )
        self._mirror(event.data.get("new_state"))
        if _within_deadband(shown, self):
            # Keep showing what was written, so small steps cannot add up
            # unseen: the next write is measured from the shown value.
            self._attr_native_value = shown[1]
            return
        self.async_write_ha_state()

    def _mirror(self, state) -> None:
        """Copy the meter's reading and unit; unavailable on any doubt."""
        # Belt and braces, not covered: those two strings also fail float()
        # below, with the same result.
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
            self._attr_native_value = None
            return
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = value
        self._attr_native_unit_of_measurement = state.attributes.get(
            "unit_of_measurement"
        )
