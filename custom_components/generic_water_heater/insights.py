"""The optional "insights" sensors: heat-up rate, Ready At, energy by source.

Each only reports; none feeds control. Created by sensor.py when its option is
on (CONF_ENABLE_HEAT_UP_RATE, CONF_ENABLE_ENERGY_SPLIT). The decision logic is
in heat_rate.py and energy_split.py, without Home Assistant; this is the
plumbing.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
import homeassistant.util.dt as dt_util

from . import DEFAULT_ACTIVE_STATE, DOMAIN, name_heater_entity
from .element_health import DRAW_W, to_watts
from .energy_split import SOURCES, EnergySplit, source_for
from .heat_rate import HeatRateLearner, ready_at
from .temperature_tracking import async_track_filtered_temperature

_LOGGER = logging.getLogger(__name__)

# Ready At is rewritten only when the estimate moves by more than this, so a
# reading every minute does not write a new timestamp every minute.
READY_AT_RESOLUTION = timedelta(minutes=2)
# How often the energy split credits a steady draw that reports no change.
ENERGY_FLUSH = timedelta(minutes=1)

HEAT_RATE_KEY = "heat_rate_learner"


def heat_rate_signal(entry_id: str) -> str:
    return f"{DOMAIN}_heat_rate_{entry_id}"


def _float(state) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


class _HeaterSensor(SensorEntity):
    """Shared plumbing: the heater's device, and its name."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry_id, name, label, key, device_identifiers, device_has_name):
        self._entry_id = entry_id
        self._device_identifiers = device_identifiers
        self._attr_name = label
        self._attr_unique_id = self.unique_id_for(entry_id, key)
        name_heater_entity(self, "sensor", name, label, device_has_name)

    @staticmethod
    def unique_id_for(entry_id: str, key: str) -> str:
        return f"{DOMAIN}_{entry_id}_{key}"

    @property
    def device_info(self):
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}
        return {"identifiers": {(DOMAIN, self._entry_id)}}


class HeatUpRateSensor(_HeaterSensor, RestoreSensor):
    """How fast this tank warms while its element draws, in C per hour.

    The element counts as drawing when the meter reads at least DRAW_W, or,
    with no meter, when the switch is on: a closed relay behind a tank
    thermostat that has opened draws nothing, and a meter tells the two apart.
    """

    KEY = "heat_up_rate"
    _attr_native_unit_of_measurement = "°C/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-chevron-up"
    _attr_suggested_display_precision = 1

    def __init__(
        self, hass, entry_id, name, heater_entity_id, sensor_entity_id,
        power_sensor, runtime, device_identifiers, device_has_name,
    ) -> None:
        super().__init__(entry_id, name, "Heat-up Rate", self.KEY, device_identifiers, device_has_name)
        self.hass = hass
        self._heater = heater_entity_id
        self._sensor = sensor_entity_id
        self._power = power_sensor
        self._runtime = runtime
        self._learner = HeatRateLearner()
        runtime[HEAT_RATE_KEY] = self._learner
        self._temperature: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"runs_learned": self._learner.runs, "drawing": self._learner.drawing}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        last_state = await self.async_get_last_state()
        if last is not None and isinstance(last.native_value, (int, float)):
            self._learner.rate = float(last.native_value)
            runs = (last_state.attributes.get("runs_learned") if last_state else None)
            self._learner.runs = int(runs) if isinstance(runs, (int, float)) else 1
        self._attr_native_value = self._rounded()
        self._temperature = _float(self.hass.states.get(self._sensor))
        self.async_on_remove(
            async_track_filtered_temperature(self.hass, self._sensor, self._async_reading)
        )
        watched = [self._heater] + ([self._power] if self._power else [])
        self.async_on_remove(
            async_track_state_change_event(self.hass, watched, self._async_element_changed)
        )
        self._element_update()

    def _rounded(self) -> float | None:
        return round(self._learner.rate, 2) if self._learner.rate is not None else None

    def _drawing(self) -> bool:
        if self._power:
            state = self.hass.states.get(self._power)
            watts = _float(state)
            if watts is None:
                return False
            return to_watts(watts, state.attributes.get("unit_of_measurement")) >= DRAW_W
        switch = self.hass.states.get(self._heater)
        return switch is not None and switch.state == STATE_ON

    @callback
    def _element_update(self) -> None:
        if self._learner.element(self._drawing(), dt_util.utcnow(), self._temperature):
            self._attr_native_value = self._rounded()
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, heat_rate_signal(self._entry_id))

    @callback
    def _async_element_changed(self, _event) -> None:
        if self._drawing() != self._learner.drawing:
            self._element_update()

    async def _async_reading(self, value, when: datetime, _attributes) -> None:
        if value is None or isinstance(value, str):
            return
        self._temperature = float(value)
        self._learner.reading(when, self._temperature)
        async_dispatcher_send(self.hass, heat_rate_signal(self._entry_id))


class ReadyAtSensor(_HeaterSensor):
    """When the tank should reach what it is heading for, at the learned rate.

    Heading for: 60 C in a disinfection cycle, otherwise the target control
    aims at (the surplus boost target while boosting). Unknown while the
    element is not drawing, once the tank is there, and until a rate is learned.
    """

    KEY = "ready_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    def __init__(self, hass, entry_id, name, runtime, device_identifiers, device_has_name) -> None:
        super().__init__(entry_id, name, "Ready At", self.KEY, device_identifiers, device_has_name)
        self.hass = hass
        self._runtime = runtime
        self._attr_native_value = None
        self._goal: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"heading_for": self._goal}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, heat_rate_signal(self._entry_id), self._recompute
            )
        )
        self._recompute()

    @callback
    def _recompute(self) -> None:
        learner: HeatRateLearner | None = self._runtime.get(HEAT_RATE_KEY)
        heater = self._runtime.get("water_heater_entity")
        goal = heater.heating_goal() if heater is not None else None
        current = getattr(heater, "native_current_temperature", None)
        estimate = ready_at(
            dt_util.utcnow(),
            current,
            goal,
            learner.rate if learner else None,
            learner.drawing if learner else False,
        )
        previous = self._attr_native_value
        moved = (
            (estimate is None) != (previous is None)
            or (
                estimate is not None
                and previous is not None
                and abs(estimate - previous) >= READY_AT_RESOLUTION
            )
        )
        if moved or goal != self._goal:
            self._attr_native_value = estimate
            self._goal = goal
            self.async_write_ha_state()


class EnergySplitTracker:
    """Integrates the meter into today's buckets; one per heater."""

    def __init__(self, hass, power_sensor, surplus, utility) -> None:
        self.hass = hass
        self._power = power_sensor
        self._surplus = surplus  # (entity_id or None, active state)
        self._utility = utility
        self.split = EnergySplit(dt_util.now().date())
        self._listeners: list = []
        self._unsubs: list = []

    def _active(self, entity_id, active_state) -> bool:
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        return state.state.strip().casefold() == (
            (active_state or DEFAULT_ACTIVE_STATE).strip().casefold()
        )

    def _watts(self) -> float | None:
        state = self.hass.states.get(self._power)
        value = _float(state)
        if value is None:
            return None
        return to_watts(value, state.attributes.get("unit_of_measurement"))

    @callback
    def advance(self, *_args) -> None:
        self.split.advance(
            dt_util.now(),
            self._watts(),
            source_for(self._active(*self._surplus), self._active(*self._utility)),
        )
        for listener in self._listeners:
            listener()

    def attach(self, listener) -> None:
        """Start on the first entity, so restored values land before credit."""
        self._listeners.append(listener)
        if self._unsubs:
            return
        watched = [self._power] + [e for e, _ in (self._surplus, self._utility) if e]
        self._unsubs = [
            async_track_state_change_event(self.hass, watched, self.advance),
            async_track_time_interval(self.hass, self.advance, ENERGY_FLUSH),
        ]
        self.advance()

    def detach(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)
        if not self._listeners:
            for unsub in self._unsubs:
                unsub()
            self._unsubs = []


class EnergyTodaySensor(_HeaterSensor, RestoreSensor):
    """Today's element energy while one source was carrying the house."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:water-boiler"

    LABELS = {
        "surplus": "Energy Today (Surplus)",
        "utility": "Energy Today (Utility)",
        "other": "Energy Today (Other)",
    }

    def __init__(self, hass, entry_id, name, source, tracker, device_identifiers, device_has_name) -> None:
        super().__init__(
            entry_id, name, self.LABELS[source], f"energy_today_{source}",
            device_identifiers, device_has_name,
        )
        self.hass = hass
        self._source = source
        self._tracker = tracker

    @property
    def native_value(self) -> float:
        return round(self._tracker.split.kwh[self._source], 3)

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day(self._tracker.split.day)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        last_state = await self.async_get_last_state()
        reset = last_state.attributes.get("last_reset") if last_state else None
        day = None
        if isinstance(reset, str) and (parsed := dt_util.parse_datetime(reset)):
            day = dt_util.as_local(parsed).date()
        if last is not None and isinstance(last.native_value, (int, float)) and day:
            self._tracker.split.restore(self._source, day, float(last.native_value))
        self._tracker.attach(self._write)
        self.async_on_remove(lambda: self._tracker.detach(self._write))

    @callback
    def _write(self) -> None:
        if self.native_value != self._attr_native_value or self._tracker.split.day != getattr(self, "_day", None):
            self._attr_native_value = self.native_value
            self._day = self._tracker.split.day
            self.async_write_ha_state()


def energy_split_unique_ids(entry_id: str) -> list[str]:
    return [_HeaterSensor.unique_id_for(entry_id, f"energy_today_{s}") for s in SOURCES]


def heat_rate_unique_ids(entry_id: str) -> list[str]:
    return [
        _HeaterSensor.unique_id_for(entry_id, HeatUpRateSensor.KEY),
        _HeaterSensor.unique_id_for(entry_id, ReadyAtSensor.KEY),
    ]
