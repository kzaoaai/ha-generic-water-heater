"""Support for generic water heater units."""
import logging
from datetime import timedelta

from homeassistant.components.water_heater import (
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
    STATE_ECO,
    STATE_PERFORMANCE,
    STATE_ELECTRIC,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, callback
from homeassistant.helpers.event import async_track_state_change_event, async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter
import homeassistant.util.dt as dt_util

from . import DOMAIN, CONF_HEATER, CONF_SENSOR, CONF_TARGET_TEMP, CONF_TEMP_STEP, CONF_COLD_TOLERANCE, CONF_HOT_TOLERANCE, CONF_TEMP_MIN, CONF_TEMP_MAX, CONF_MIN_ON_DURATION, CONF_MIN_OFF_DURATION, CONF_ECO_ENTITY, CONF_ECO_VALUE

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Generic Water Heater"


async def async_setup_platform(
    hass, hass_config, async_add_entities, discovery_info=None
):
    """Set up the generic water_heater devices."""
    entities = []

    for config in discovery_info:
        name = config[CONF_NAME]
        heater_entity_id = config[CONF_HEATER]
        sensor_entity_id = config[CONF_SENSOR]
        target_temp = config.get(CONF_TARGET_TEMP)
        target_temp_step = config.get(CONF_TEMP_STEP, 1.0)
        cold_tolerance = config.get(CONF_COLD_TOLERANCE, 0.0)
        hot_tolerance = config.get(CONF_HOT_TOLERANCE, 0.0)
        min_temp = config.get(CONF_TEMP_MIN, 15.0)
        max_temp = config.get(CONF_TEMP_MAX, 80.0)
        min_on_duration = config.get(CONF_MIN_ON_DURATION, config.get("min_cycle_duration"))
        min_off_duration = config.get(CONF_MIN_OFF_DURATION, config.get("min_cycle_duration"))
        eco_entity_id = config.get(CONF_ECO_ENTITY)
        eco_value = config.get(CONF_ECO_VALUE)
        unit = hass.config.units.temperature_unit

        registry = er.async_get(hass)
        entity_entry = registry.async_get(heater_entity_id)
        device_identifiers = None
        if entity_entry and entity_entry.device_id:
            device_registry = dr.async_get(hass)
            device_entry = device_registry.async_get(entity_entry.device_id)
            if device_entry:
                device_identifiers = device_entry.identifiers

        entities.append(
            GenericWaterHeater(
                name,
                heater_entity_id,
                sensor_entity_id,
                target_temp,
                target_temp_step,
                cold_tolerance,
                hot_tolerance,
                min_temp,
                max_temp,
                min_on_duration,
                min_off_duration,
                eco_entity_id,
                eco_value,
                unit,
                config_entry_id=None,
                device_identifiers=device_identifiers,
            )
        )

    async_add_entities(entities)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up a water heater from a config entry."""
    # merge entry data and options so options override data
    data = {**entry.data, **getattr(entry, "options", {})}
    name = data.get(CONF_NAME, DEFAULT_NAME)
    heater_entity_id = data.get(CONF_HEATER)
    sensor_entity_id = data.get(CONF_SENSOR)
    target_temp = data.get(CONF_TARGET_TEMP)
    target_temp_step = data.get(CONF_TEMP_STEP)
    cold_tolerance = data.get(CONF_COLD_TOLERANCE, 0.0)
    hot_tolerance = data.get(CONF_HOT_TOLERANCE, 0.0)
    min_temp = data.get(CONF_TEMP_MIN, 15.0)
    max_temp = data.get(CONF_TEMP_MAX, 80.0)
    min_on_duration = data.get(CONF_MIN_ON_DURATION, data.get("min_cycle_duration"))
    min_off_duration = data.get(CONF_MIN_OFF_DURATION, data.get("min_cycle_duration"))
    eco_entity_id = data.get(CONF_ECO_ENTITY)
    eco_value = data.get(CONF_ECO_VALUE)
    unit = hass.config.units.temperature_unit

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_entry = registry.async_get(heater_entity_id)
    device_identifiers = None
    current_device_id = None

    if min_on_duration is not None and isinstance(min_on_duration, dict):
        min_on_duration = cv.time_period(min_on_duration)
        
    if min_off_duration is not None and isinstance(min_off_duration, dict):
        min_off_duration = cv.time_period(min_off_duration)

    if entity_entry and entity_entry.device_id:
        device_entry = device_registry.async_get(entity_entry.device_id)
        if device_entry:
            device_identifiers = device_entry.identifiers
            current_device_id = device_entry.id

    # Cleanup old device links for this config entry
    linked_devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    for dev in linked_devices:
        if current_device_id and dev.id != current_device_id:
            device_registry.async_update_device(dev.id, remove_config_entry_id=entry.entry_id)
        elif not current_device_id:
            # If no physical device is linked, ensure we don't keep links to old physical devices.
            # Check if it's the standalone device (identifier matches entry_id)
            is_standalone = any(dom == DOMAIN and ident == entry.entry_id for dom, ident in dev.identifiers)
            if not is_standalone:
                device_registry.async_update_device(dev.id, remove_config_entry_id=entry.entry_id)

    async_add_entities(
        [
            GenericWaterHeater(
                name,
                heater_entity_id,
                sensor_entity_id,
                target_temp,
                target_temp_step,
                cold_tolerance,
                hot_tolerance,
                min_temp,
                max_temp,
                min_on_duration,
                min_off_duration,
                eco_entity_id,
                eco_value,
                unit,
                config_entry_id=entry.entry_id,
                device_identifiers=device_identifiers,
            )
        ]
    )
    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry for this platform."""
    # Entities are removed automatically when the config entry is removed/unloaded
    return True


class GenericWaterHeater(WaterHeaterEntity, RestoreEntity):
    """Representation of a generic water_heater device."""

    def __init__(
        self,
        name,
        heater_entity_id,
        sensor_entity_id,
        target_temp,
        target_temp_step,
        cold_tolerance,
        hot_tolerance,
        min_temp,
        max_temp,
        min_on_duration,
        min_off_duration,
        eco_entity_id,
        eco_value,
        unit,
        config_entry_id=None,
        device_identifiers=None,
    ):
        """Initialize the water_heater device."""
        self._attr_name = name
        self.heater_entity_id = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self._attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.OPERATION_MODE | WaterHeaterEntityFeature.ON_OFF
        self._target_temperature = target_temp
        self._target_temperature_step = target_temp_step
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._min_on_duration = min_on_duration if min_on_duration else timedelta(seconds=0)
        self._min_off_duration = min_off_duration if min_off_duration else timedelta(seconds=120)
        self._eco_entity_id = eco_entity_id
        self._eco_value = eco_value
        self._unit_of_measurement = unit
        self._current_operation = STATE_ELECTRIC
        self._saved_operation_mode = STATE_ELECTRIC
        self._current_temperature = None
        self._operation_list = [
            STATE_ELECTRIC,
            STATE_OFF,
            STATE_PERFORMANCE,
        ]
        if eco_entity_id and eco_value:
            self._operation_list.append(STATE_ECO)
        self._attr_available = False
        self._attr_should_poll = False
        self._device_identifiers = device_identifiers
        self._last_commanded_switch_state = None
        self._timer_handle = None
        self._last_switch_change_time = None
        self._cooldown_timer = None
        # device/unique id
        # prefer config_entry_id (when created via UI) otherwise fall back to heater entity id
        self._device_identifier = config_entry_id or heater_entity_id
        # expose unique_id for the entity
        try:
            self._attr_unique_id = f"{DOMAIN}_{self._device_identifier}"
        except Exception:
            pass

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {
                "identifiers": self._device_identifiers,
            }
        return {
            "identifiers": {(DOMAIN, self._device_identifier)},
            "name": self._attr_name,
        }

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""
        return {
            "hvac_action": self.hvac_action
        }

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported."""
        if self._current_operation == STATE_OFF:
            return "off"
        heater = self.hass.states.get(self.heater_entity_id)
        if heater and heater.state == STATE_ON:
            return "heating"
        return "idle"

    @property
    def current_temperature(self):
        """Return current temperature."""
        return self._current_temperature

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return self._target_temperature_step

    @property
    def current_operation(self):
        """Return current operation ie. on, off."""
        return self._current_operation

    @property
    def operation_list(self):
        """Return the list of available operation modes."""
        return self._operation_list

    @property
    def min_temp(self):
        """Return the minimum targetable temperature."""
        """If the min temperature is not set on the config, returns the HA default for Water Heaters."""
        if self._min_temp is None:
            return TemperatureConverter.convert(DEFAULT_MIN_TEMP, UnitOfTemperature.FAHRENHEIT, self._unit_of_measurement) 
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum targetable temperature."""
        """If the max temperature is not set on the config, returns the HA default for Water Heaters."""
        if self._max_temp is None:
            return TemperatureConverter.convert(DEFAULT_MAX_TEMP, UnitOfTemperature.FAHRENHEIT, self._unit_of_measurement) 
        return self._max_temp

    async def async_set_temperature(self, **kwargs):
        """Set new target temperatures."""
        self._target_temperature = kwargs.get(ATTR_TEMPERATURE)
        _LOGGER.debug("%s: async_set_temperature -> target=%s", self.name, self._target_temperature)
        await self._async_control_heating()

    async def async_set_operation_mode(self, operation_mode):
        """Set new operation mode."""
        if operation_mode != STATE_OFF:
            self._saved_operation_mode = operation_mode
        self._current_operation = operation_mode
        _LOGGER.debug("%s: async_set_operation_mode -> mode=%s", self.name, self._current_operation)
        await self._async_control_heating()

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        mode = getattr(self, "_saved_operation_mode", STATE_ELECTRIC)
        if mode == STATE_OFF:
            mode = STATE_ELECTRIC
        await self.async_set_operation_mode(mode)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self.async_set_operation_mode(STATE_OFF)

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._async_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_switch_changed
            )
        )
        
        if self._eco_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._eco_entity_id], self._async_eco_sensor_changed
                )
            )

        old_state = await self.async_get_last_state()
        if old_state is not None:
            if old_state.attributes.get(ATTR_TEMPERATURE) is not None:
                self._target_temperature = float(old_state.attributes.get(ATTR_TEMPERATURE))
            self._current_operation = old_state.state
            # Map legacy "on" state to "electric"
            if self._current_operation == STATE_ON:
                self._current_operation = STATE_ELECTRIC
            
            if self._current_operation not in self._operation_list:
                self._current_operation = STATE_OFF
            elif self._current_operation != STATE_OFF:
                self._saved_operation_mode = self._current_operation
        
        # Ensure target temperature is set if not restored (e.g. new entity)
        if self._target_temperature is None:
            self._target_temperature = self.min_temp

        temp_sensor = self.hass.states.get(self.sensor_entity_id)
        if temp_sensor and temp_sensor.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._current_temperature = float(temp_sensor.state)

        heater_switch = self.hass.states.get(self.heater_entity_id)
        if heater_switch and heater_switch.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._attr_available = True
            self._last_commanded_switch_state = heater_switch.state
        
        await self._async_control_heating()
        self.async_write_ha_state()

    async def _async_sensor_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Failsafe
            _LOGGER.warning(
                "No Temperature information, entering Failsafe, turning off heater %s",
                self.heater_entity_id,
            )
            await self._async_heater_turn_off()
            self._current_temperature = None
        else:
            self._current_temperature = float(new_state.state)

        _LOGGER.debug(
            "%s: sensor changed -> current_temperature=%s, target=%s, cold_tolerance=%s, hot_tolerance=%s",
            self.name,
            self._current_temperature,
            self._target_temperature,
            self._cold_tolerance,
            self._hot_tolerance,
        )

        await self._async_control_heating()

    async def _async_eco_sensor_changed(self, event):
        """Handle eco sensor state changes."""
        new_state = event.data.get("new_state")
        _LOGGER.debug("Eco sensor changed: %s", new_state)
        if self._current_operation == STATE_ECO:
            await self._async_control_heating()

    @callback
    def _async_switch_changed(self, event):
        """Handle heater switch state changes."""
        new_state = event.data.get("new_state")
        _LOGGER.debug("New switch state = %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
        else:
            self._attr_available = True
            _LOGGER.debug("%s became Available", self.name)
            
            self._last_switch_change_time = dt_util.utcnow()
            if (
                self._last_commanded_switch_state is not None
                and new_state.state != self._last_commanded_switch_state
            ):
                _LOGGER.debug("Manual switch override detected: %s", new_state.state)
                if new_state.state == STATE_ON:
                    if self._current_operation not in (STATE_ELECTRIC, STATE_ECO, STATE_PERFORMANCE):
                         self._current_operation = getattr(self, "_saved_operation_mode", STATE_ELECTRIC)
                         if self._current_operation == STATE_OFF:
                             self._current_operation = STATE_ELECTRIC
                elif new_state.state == STATE_OFF:
                    self._current_operation = STATE_OFF
                
                if self._timer_handle:
                    self._timer_handle()
                self._timer_handle = async_call_later(self.hass, 5, self._async_control_heating_callback)

        self.async_write_ha_state()

    async def _async_control_heating(self):
        """Check if we need to turn heating on or off."""
        _LOGGER.debug(
            "%s: control_heating start -> operation=%s, current_temperature=%s, target=%s, cold_tolerance=%s, hot_tolerance=%s",
            self.name,
            self._current_operation,
            self._current_temperature,
            self._target_temperature,
            self._cold_tolerance,
            self._hot_tolerance,
        )

        # If the water heater mode is explicitly OFF, ensure underlying switch is off
        if self._current_operation == STATE_OFF:
            _LOGGER.debug("%s: operation is OFF, turning underlying switch off", self.name)
            await self._async_heater_turn_off()
            self.async_write_ha_state()
            return

        # Logic for PERFORMANCE: Heat regardless of temperature
        if self._current_operation == STATE_PERFORMANCE:
            _LOGGER.debug("%s: operation is PERFORMANCE, turning ON", self.name)
            await self._async_heater_turn_on()
            self.async_write_ha_state()
            return

        # Logic for ECO: Check additional condition
        if self._current_operation == STATE_ECO:
            eco_condition_met = True
            if self._eco_entity_id:
                eco_state = self.hass.states.get(self._eco_entity_id)
                if eco_state is None or str(eco_state.state) != str(self._eco_value):
                    eco_condition_met = False
            
            if not eco_condition_met:
                _LOGGER.debug("%s: operation is ECO but condition not met, turning OFF", self.name)
                await self._async_heater_turn_off()
                self.async_write_ha_state()
                return

        # If we don't have the required temperature information, just update state
        if (
            self._current_temperature is None
            or self._target_temperature is None
        ):
            _LOGGER.debug("%s: missing temperature/target, skipping control", self.name)
            self.async_write_ha_state()
            return

        # Control heating based on tolerance
        # Logic: Turn ON if temp <= target - cold_tolerance. Turn OFF if temp >= target + hot_tolerance.
        if self._current_temperature <= (self._target_temperature - self._cold_tolerance) and self._current_operation in (STATE_ELECTRIC, STATE_ECO):
            _LOGGER.debug("%s: current <= (target - cold_tolerance) -> turning ON", self.name)
            await self._async_heater_turn_on()
        elif self._current_temperature >= (self._target_temperature + self._hot_tolerance):
            _LOGGER.debug("%s: current >= (target + hot_tolerance) -> turning OFF", self.name)
            await self._async_heater_turn_off()
        # Else: stay in current state (hysteresis band)

        self.async_write_ha_state()

    async def _async_control_heating_callback(self, _now):
        """Callback for delayed control heating."""
        self._timer_handle = None
        self._cooldown_timer = None
        await self._async_control_heating()

    async def _async_heater_turn_on(self):
        """Turn heater toggleable device on."""
        now = dt_util.utcnow()
        if self._last_switch_change_time:
            delta = now - self._last_switch_change_time
            if delta < self._min_off_duration:
                _LOGGER.debug("Cooldown active (min_off_duration), delaying turn_on")
                if self._cooldown_timer:
                    self._cooldown_timer()
                self._cooldown_timer = async_call_later(self.hass, (self._min_off_duration - delta).total_seconds(), self._async_control_heating_callback)
                return

        self._last_commanded_switch_state = STATE_ON
        heater = self.hass.states.get(self.heater_entity_id)
        if heater is None or heater.state == STATE_ON:
            return

        _LOGGER.debug("Turning on heater %s", self.heater_entity_id)
        self._last_switch_change_time = now
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_ON, data, context=self._context
        )

    async def _async_heater_turn_off(self):
        """Turn heater toggleable device off."""
        now = dt_util.utcnow()
        if self._last_switch_change_time:
            delta = now - self._last_switch_change_time
            if delta < self._min_on_duration:
                _LOGGER.debug("Cooldown active (min_on_duration), delaying turn_off")
                if self._cooldown_timer:
                    self._cooldown_timer()
                self._cooldown_timer = async_call_later(self.hass, (self._min_on_duration - delta).total_seconds(), self._async_control_heating_callback)
                return

        self._last_commanded_switch_state = STATE_OFF
        heater = self.hass.states.get(self.heater_entity_id)
        if heater is None or heater.state == STATE_OFF:
            return

        _LOGGER.debug("Turning off heater %s", self.heater_entity_id)
        self._last_switch_change_time = now
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_OFF, data, context=self._context
        )
