"""Support for generic water heater units."""
import logging
from datetime import timedelta

from homeassistant.components.water_heater import (
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
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
from homeassistant.core import DOMAIN as HA_DOMAIN, Event, EventStateChangedData, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.event import (
    TrackTemplate,
    TrackTemplateResult,
    async_call_later,
    async_track_state_change_event,
    async_track_template_result,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.template import Template, result_as_boolean

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter
import homeassistant.util.dt as dt_util

from . import (
    CONF_COLD_TOLERANCE,
    CONF_DEBUG_LOGGING,
    CONF_ECO_TEMPLATE,
    CONF_HEATER,
    CONF_HOT_TOLERANCE,
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_SENSOR,
    CONF_TARGET_TEMP,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DOMAIN,
    smart_eco_signal,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Generic Water Heater"


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
    eco_template = (data.get(CONF_ECO_TEMPLATE) or "").strip() or None
    debug_logging = data.get(CONF_DEBUG_LOGGING, False)
    unit = hass.config.units.temperature_unit
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    if runtime.get("smart_eco_enabled") is None:
        # Smart Eco defaults to enabled when a template exists; otherwise disabled.
        runtime["smart_eco_enabled"] = eco_template is not None

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

    entity = GenericWaterHeater(
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
        eco_template,
        debug_logging,
        unit,
        runtime,
        config_entry_id=entry.entry_id,
        device_identifiers=device_identifiers,
    )
    runtime["water_heater_entity"] = entity
    async_add_entities([entity])
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
        eco_template,
        debug_logging,
        unit,
        runtime,
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
        self._eco_template = Template(eco_template, None) if eco_template else None
        self._runtime = runtime
        self._smart_eco_enabled = bool(runtime.get("smart_eco_enabled", eco_template is not None))
        self._debug_logging = bool(debug_logging)
        self._eco_condition_met = False
        self._unit_of_measurement = unit
        self._current_operation = STATE_ELECTRIC
        self._current_temperature = None
        self._operation_list = [
            STATE_ELECTRIC,
            STATE_OFF,
            STATE_PERFORMANCE,
        ]
        self._attr_available = False
        self._attr_should_poll = False
        self._device_identifiers = device_identifiers
        self._last_commanded_switch_state = None
        self._last_switch_change_time = None
        self._cooldown_timer = None
        self._pending_switch_state = None
        self._last_debug_hvac_action = None
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
            "hvac_action": self.hvac_action,
            "smart_eco_enabled": self._smart_eco_enabled,
            "smart_eco_condition_met": self._eco_condition_met,
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
        old_mode = self._current_operation
        self._current_operation = operation_mode
        _LOGGER.debug("%s: async_set_operation_mode -> mode=%s", self.name, self._current_operation)
        if old_mode != operation_mode:
            self._debug_log("operation mode changed: %s -> %s", old_mode, operation_mode)
        await self._async_control_heating()

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        await self.async_set_smart_eco_enabled(False, source="turn_on", recalculate=False)
        await self.async_set_operation_mode(STATE_ELECTRIC)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        await self.async_set_smart_eco_enabled(False, source="turn_off", recalculate=False)
        await self.async_set_operation_mode(STATE_OFF)

    async def async_set_smart_eco_enabled(
        self,
        enabled: bool,
        source: str,
        recalculate: bool = True,
    ) -> None:
        """Enable/disable Smart Eco policy."""
        if self._smart_eco_enabled == enabled:
            return

        self._smart_eco_enabled = enabled
        self._runtime["smart_eco_enabled"] = enabled
        self._debug_log("smart eco changed: enabled=%s (source=%s)", enabled, source)
        async_dispatcher_send(self.hass, smart_eco_signal(self._device_identifier), enabled)
        if recalculate:
            await self._async_control_heating()

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

        if self._eco_template:
            self._eco_template.hass = self.hass
            info = async_track_template_result(
                self.hass,
                [TrackTemplate(self._eco_template, None)],
                self._async_eco_template_changed,
            )
            self.async_on_remove(info.async_remove)
            info.async_refresh()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            if old_state.attributes.get(ATTR_TEMPERATURE) is not None:
                self._target_temperature = float(old_state.attributes.get(ATTR_TEMPERATURE))
            self._current_operation = old_state.state
            if self._current_operation == STATE_ON:
                self._current_operation = STATE_ELECTRIC
            if self._current_operation not in self._operation_list and self._current_operation == "eco":
                # Legacy mode restore: eco no longer exists as an operation mode.
                self._current_operation = STATE_OFF

            if self._current_operation not in self._operation_list:
                self._current_operation = STATE_OFF
            restored_smart_eco = old_state.attributes.get("smart_eco_enabled")
            if isinstance(restored_smart_eco, bool):
                self._smart_eco_enabled = restored_smart_eco
                self._runtime["smart_eco_enabled"] = restored_smart_eco
        
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

        self._async_refresh_eco_condition()
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
        self._debug_log(
            "sensor update: temp_sensor_entity=%s, current_temperature=%.1f, target=%.1f, mode=%s",
            self.sensor_entity_id,
            self._current_temperature if self._current_temperature is not None else 0,
            self._target_temperature if self._target_temperature is not None else 0,
            self._current_operation,
        )

        await self._async_control_heating()

    @callback
    def _async_refresh_eco_condition(self, result=None):
        """Refresh the current eco condition state."""
        previous = self._eco_condition_met
        if self._eco_template is None:
            self._eco_condition_met = False
            if previous != self._eco_condition_met:
                self._debug_log("eco condition changed: %s -> %s", previous, self._eco_condition_met)
            return

        if result is None:
            try:
                result = self._eco_template.async_render(parse_result=False)
            except TemplateError as err:
                _LOGGER.warning("%s: eco template render failed: %s", self.name, err)
                self._eco_condition_met = False
                if previous != self._eco_condition_met:
                    self._debug_log("eco condition changed: %s -> %s", previous, self._eco_condition_met)
                return

        self._eco_condition_met = result_as_boolean(result)
        if previous != self._eco_condition_met:
            self._debug_log("eco condition evaluated: template_result=%s, meets_condition=%s", result, self._eco_condition_met)
            self._debug_log("eco condition changed: %s -> %s", previous, self._eco_condition_met)

    async def _async_eco_template_changed(
        self,
        event: Event[EventStateChangedData] | None,
        updates: list[TrackTemplateResult],
    ) -> None:
        """Handle eco template updates."""
        update = updates.pop()
        result = update.result

        if isinstance(result, TemplateError):
            _LOGGER.warning("%s: eco template update failed: %s", self.name, result)
            self._eco_condition_met = False
        else:
            self._async_refresh_eco_condition(result)

        if event:
            self.async_set_context(event.context)

        if self._smart_eco_enabled:
            await self._async_control_heating()

    @callback
    def _async_switch_changed(self, event):
        """Handle heater switch state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        _LOGGER.debug("New switch state = %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
        else:
            self._attr_available = True
            _LOGGER.debug("%s became Available", self.name)
            self._debug_log(
                "switch changed: new_state=%s, last_commanded=%s, mode=%s",
                new_state.state,
                self._last_commanded_switch_state,
                self._current_operation,
            )

            self._last_switch_change_time = dt_util.utcnow()
            state_changed = old_state is not None and old_state.state != new_state.state
            had_pending = self._pending_switch_state is not None

            # A real switch state change supersedes any delayed action that was queued.
            if state_changed and self._cooldown_timer is not None:
                self._cooldown_timer()
                self._cooldown_timer = None
                self._debug_log(
                    "cooldown timer canceled due to switch state change: %s -> %s",
                    old_state.state,
                    new_state.state,
                )

            if (
                self._last_commanded_switch_state is not None
                and new_state.state != self._last_commanded_switch_state
            ):
                _LOGGER.debug("Manual switch override detected: %s", new_state.state)
                self.hass.async_create_task(
                    self._async_handle_manual_switch_override(new_state.state)
                )
            elif had_pending and state_changed:
                # If a delayed ON was pending and user flips back to OFF, honor OFF explicitly.
                if self._pending_switch_state == STATE_ON and new_state.state == STATE_OFF:
                    _LOGGER.debug(
                        "Manual OFF detected while delayed ON was pending; forcing OFF mode",
                    )
                    self._debug_log(
                        "manual OFF superseded delayed ON intent -> forcing OFF mode",
                    )
                    self.hass.async_create_task(
                        self._async_handle_manual_switch_override(new_state.state)
                    )
                else:
                    _LOGGER.debug(
                        "Manual switch override detected from pending action: %s",
                        new_state.state,
                    )
                    self.hass.async_create_task(
                        self._async_handle_manual_switch_override(new_state.state)
                    )

            if state_changed:
                self._pending_switch_state = None

        self._debug_log_hvac_action("switch state update")
        self.async_write_ha_state()

    async def _async_handle_manual_switch_override(self, new_switch_state: str) -> None:
        """Translate manual switch actions into operation mode intent."""
        self._debug_log("=== manual override detected: new_state=%s, current_mode=%s ===", new_switch_state, self._current_operation)
        await self.async_set_smart_eco_enabled(False, source="manual_switch", recalculate=False)

        if new_switch_state == STATE_ON:
            if self._current_operation == STATE_OFF:
                # Manual ON from OFF: use threshold-aware remap.
                if self._electric_mode_wants_heating():
                    self._log_debug_decision(
                        "manual switch ON from OFF -> ELECTRIC (current=%.1f <= target-cold=%.1f)",
                        self._current_temperature,
                        self._target_temperature - self._cold_tolerance,
                    )
                    await self.async_set_operation_mode(STATE_ELECTRIC)
                else:
                    lower_threshold = None
                    if self._target_temperature is not None:
                        lower_threshold = self._target_temperature - self._cold_tolerance
                    self._log_debug_decision(
                        "manual switch ON from OFF -> PERFORMANCE (current=%s > target-cold=%s, forcing heat)",
                        self._current_temperature,
                        lower_threshold,
                    )
                    await self.async_set_operation_mode(STATE_PERFORMANCE)
            elif self._current_operation == STATE_ELECTRIC:
                # Manual ON while electric would stay idle means user wants immediate heat.
                if not self._electric_mode_wants_heating():
                    self._log_debug_decision(
                        "manual switch ON from ELECTRIC -> PERFORMANCE (electric idle at current=%s, target-cold=%s)",
                        self._current_temperature,
                        None
                        if self._target_temperature is None
                        else self._target_temperature - self._cold_tolerance,
                    )
                    await self.async_set_operation_mode(STATE_PERFORMANCE)
                else:
                    self._log_debug_decision(
                        "manual switch ON while ELECTRIC already requests heat (rare mismatch path; current=%s <= target-cold=%s); no mode remap needed",
                        self._current_temperature,
                        self._target_temperature - self._cold_tolerance,
                    )
            elif self._current_operation == STATE_PERFORMANCE:
                self._log_debug_decision(
                    "manual switch ON while PERFORMANCE; no mode remap needed",
                )
            else:
                self._log_debug_decision(
                    "manual switch ON while mode=%s; no mode remap needed",
                    self._current_operation,
                )
            return

        if new_switch_state == STATE_OFF:
            self._log_debug_decision(
                "manual switch OFF in %s -> OFF (hard manual intent, bypass eco remap)",
                self._current_operation,
            )
            await self.async_set_operation_mode(STATE_OFF)

    def _electric_mode_wants_heating(self) -> bool:
        """Return whether ELECTRIC mode would currently request heat."""
        if self._current_temperature is None or self._target_temperature is None:
            return False

        return self._current_temperature <= (
            self._target_temperature - self._cold_tolerance
        )

    def _debug_log(self, message: str, *args) -> None:
        """Log debug diagnostics when debug mode is enabled."""
        if not self._debug_logging:
            return
        _LOGGER.debug("%s [debug]: " + message, self.name, *args)

    def _debug_log_hvac_action(self, source: str) -> None:
        """Log hvac action transitions when debug mode is enabled."""
        if not self._debug_logging:
            return
        action = self.hvac_action
        if action == self._last_debug_hvac_action:
            return
        self._debug_log(
            "hvac_action changed: %s -> %s (%s)",
            self._last_debug_hvac_action,
            action,
            source,
        )
        self._last_debug_hvac_action = action

    def _log_debug_decision(self, message: str, *args) -> None:
        """Log manual override decision details when debug mode is enabled."""
        self._debug_log(message, *args)

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
        _LOGGER.debug("%s: debug_logging flag is: %s", self.name, self._debug_logging)
        self._debug_log(
            "=== control_heating entry: mode=%s, current_temp=%s, target=%s, smart_eco_enabled=%s, eco_condition_met=%s ===",
            self._current_operation,
            self._current_temperature,
            self._target_temperature,
            self._smart_eco_enabled,
            self._eco_condition_met,
        )

        # If the water heater mode is explicitly OFF, ensure underlying switch is off
        if self._current_operation == STATE_OFF:
            _LOGGER.debug("%s: operation is OFF, turning underlying switch off", self.name)
            self._debug_log("decision: mode OFF -> switch OFF")
            await self._async_heater_turn_off()
            self._debug_log_hvac_action("mode OFF")
            self.async_write_ha_state()
            return

        smart_eco_active = self._smart_eco_enabled and self._eco_template is not None
        if smart_eco_active and not self._eco_condition_met:
            self._debug_log(
                "decision: smart eco blocks heating (template condition false) -> switch OFF",
            )
            await self._async_heater_turn_off()
            self._debug_log_hvac_action("smart eco blocked")
            self.async_write_ha_state()
            return

        # Logic for PERFORMANCE: Heat continuously while not blocked by Smart Eco.
        if self._current_operation == STATE_PERFORMANCE:
            _LOGGER.debug("%s: operation is PERFORMANCE, turning ON", self.name)
            self._debug_log("decision: mode PERFORMANCE -> switch ON")
            await self._async_heater_turn_on()
            self._debug_log_hvac_action("mode PERFORMANCE")
            self.async_write_ha_state()
            return

        # If we don't have the required temperature information, just update state
        if (
            self._current_temperature is None
            or self._target_temperature is None
        ):
            _LOGGER.debug("%s: missing temperature/target, skipping control", self.name)
            self._debug_log("decision: skip control due to missing temperature or target")
            self._debug_log_hvac_action("missing temperature/target")
            self.async_write_ha_state()
            return

        # Control heating based on tolerance
        # Logic: Turn ON if temp <= target - cold_tolerance. Turn OFF if temp >= target + hot_tolerance.
        lower_threshold = self._target_temperature - self._cold_tolerance
        upper_threshold = self._target_temperature + self._hot_tolerance
        
        self._debug_log(
            "thresholds: current=%.1f, lower=%.1f (target-%.1f), upper=%.1f (target+%.1f)",
            self._current_temperature,
            lower_threshold,
            self._cold_tolerance,
            upper_threshold,
            self._hot_tolerance,
        )
        
        if self._current_temperature <= lower_threshold and self._current_operation == STATE_ELECTRIC:
            _LOGGER.debug("%s: current <= (target - cold_tolerance) -> turning ON", self.name)
            self._debug_log("decision: current %.1f <= threshold %.1f (%.1f°C gap) -> switch ON", self._current_temperature, lower_threshold, lower_threshold - self._current_temperature)
            await self._async_heater_turn_on()
        elif self._current_temperature >= upper_threshold:
            _LOGGER.debug("%s: current >= (target + hot_tolerance) -> turning OFF", self.name)
            self._debug_log("decision: current %.1f >= threshold %.1f (%.1f°C gap) -> switch OFF", self._current_temperature, upper_threshold, self._current_temperature - upper_threshold)
            await self._async_heater_turn_off()
        else:
            # Else: stay in current state (hysteresis band)
            self._debug_log("decision: within hysteresis band [%.1f, %.1f], maintaining current state", lower_threshold, upper_threshold)

        self._debug_log_hvac_action("hysteresis control")
        self.async_write_ha_state()

    async def _async_control_heating_callback(self, _now):
        """Callback for delayed control heating."""
        self._cooldown_timer = None
        self._pending_switch_state = None
        self._debug_log("cooldown timer fired: retrying control heating")
        await self._async_control_heating()

    async def _async_heater_turn_on(self):
        """Turn heater toggleable device on."""
        now = dt_util.utcnow()
        if self._last_switch_change_time:
            delta = now - self._last_switch_change_time
            if delta < self._min_off_duration:
                _LOGGER.debug("Cooldown active (min_off_duration), delaying turn_on")
                remaining = (self._min_off_duration - delta).total_seconds()
                self._pending_switch_state = STATE_ON
                # Mark intended switch target now so opposite manual toggles are treated as overrides.
                self._last_commanded_switch_state = STATE_ON
                self._debug_log(
                    "cooldown: ON blocked by min_off_duration (elapsed=%.1fs, required=%.1fs, remaining=%.1fs); retrying in %.1fs",
                    delta.total_seconds(),
                    self._min_off_duration.total_seconds(),
                    remaining,
                    remaining,
                )
                if self._cooldown_timer:
                    self._cooldown_timer()
                self._debug_log("cooldown timer started for turn_on retry (%.1fs)", remaining)
                self._cooldown_timer = async_call_later(self.hass, remaining, self._async_control_heating_callback)
                return

            self._pending_switch_state = None
        self._last_commanded_switch_state = STATE_ON
        heater = self.hass.states.get(self.heater_entity_id)
        if heater is None or heater.state == STATE_ON:
            return

        _LOGGER.debug("Turning on heater %s", self.heater_entity_id)
        self._debug_log("service call: turn_on entity_id=%s", self.heater_entity_id)
        self._last_switch_change_time = now
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_ON, data, context=self._context
        )
        self._debug_log("service call completed: turn_on entity_id=%s", self.heater_entity_id)

    async def _async_heater_turn_off(self):
        """Turn heater toggleable device off."""
        now = dt_util.utcnow()
        if self._last_switch_change_time:
            delta = now - self._last_switch_change_time
            if delta < self._min_on_duration:
                _LOGGER.debug("Cooldown active (min_on_duration), delaying turn_off")
                remaining = (self._min_on_duration - delta).total_seconds()
                self._pending_switch_state = STATE_OFF
                # Mark intended switch target now so opposite manual toggles are treated as overrides.
                self._last_commanded_switch_state = STATE_OFF
                self._debug_log(
                    "cooldown: OFF blocked by min_on_duration (elapsed=%.1fs, required=%.1fs, remaining=%.1fs); retrying in %.1fs",
                    delta.total_seconds(),
                    self._min_on_duration.total_seconds(),
                    remaining,
                    remaining,
                )
                if self._cooldown_timer:
                    self._cooldown_timer()
                self._debug_log("cooldown timer started for turn_off retry (%.1fs)", remaining)
                self._cooldown_timer = async_call_later(self.hass, remaining, self._async_control_heating_callback)
                return

            self._pending_switch_state = None
        self._last_commanded_switch_state = STATE_OFF
        heater = self.hass.states.get(self.heater_entity_id)
        if heater is None or heater.state == STATE_OFF:
            return

        _LOGGER.debug("Turning off heater %s", self.heater_entity_id)
        self._debug_log("service call: turn_off entity_id=%s", self.heater_entity_id)
        self._last_switch_change_time = now
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_OFF, data, context=self._context
        )
        self._debug_log("service call completed: turn_off entity_id=%s", self.heater_entity_id)