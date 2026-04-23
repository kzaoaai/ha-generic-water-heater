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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from . import (
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_HEATER,
    CONF_SENSOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
_WINDOW = timedelta(days=7)
_ATTR_MAX_RECORDED_AT = "highest_recorded_at"
_ATTR_SAMPLES_TRACKED = "samples_tracked"


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
    if not data.get(CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR, False):
        return

    heater_entity_id = data.get(CONF_HEATER)
    source_sensor_entity_id = data.get(CONF_SENSOR)
    name = data.get(CONF_NAME)

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_entry = registry.async_get(heater_entity_id)
    device_identifiers = None

    if entity_entry and entity_entry.device_id:
        device_entry = device_registry.async_get(entity_entry.device_id)
        if device_entry:
            device_identifiers = device_entry.identifiers

    async_add_entities(
        [
            MaxTemperatureHistorySensor(
                name=name,
                source_sensor_entity_id=source_sensor_entity_id,
                device_identifier=entry.entry_id,
                device_identifiers=device_identifiers,
            )
        ]
    )


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

        if not device_identifiers and name:
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