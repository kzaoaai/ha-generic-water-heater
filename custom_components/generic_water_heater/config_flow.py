"""Config flow for Generic Water Heater integration."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import selector

from . import DOMAIN, CONF_HEATER, CONF_SENSOR, CONF_TEMP_STEP, CONF_COLD_TOLERANCE, CONF_HOT_TOLERANCE, CONF_TEMP_MIN, CONF_TEMP_MAX, CONF_MIN_ON_DURATION, CONF_MIN_OFF_DURATION, CONF_ECO_ENTITY, CONF_ECO_VALUE


class GenericWaterHeaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Generic Water Heater."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return options flow handler for the config entry (compat helper)."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # Create the config entry
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_HEATER): selector({"entity": {"domain": ["switch", "input_boolean"]}}),
                vol.Required(CONF_SENSOR): selector({"entity": {"domain": "sensor", "device_class": "temperature"}}),
                vol.Optional(CONF_TEMP_STEP, default=1.0): vol.Coerce(float),
                vol.Optional(CONF_COLD_TOLERANCE, default=0.0): vol.Coerce(float),
                vol.Optional(CONF_HOT_TOLERANCE, default=0.0): vol.Coerce(float),
                vol.Optional(CONF_TEMP_MIN, default=15.0): vol.Coerce(float),
                vol.Optional(CONF_TEMP_MAX, default=80.0): vol.Coerce(float),
                vol.Optional(CONF_MIN_ON_DURATION, default={"seconds": 0}): selector({"duration": {}}),
                vol.Optional(CONF_MIN_OFF_DURATION, default={"seconds": 120}): selector({"duration": {}}),
                vol.Optional(CONF_ECO_ENTITY): selector({"entity": {"domain": ["sensor", "binary_sensor"]}}),
                vol.Optional(CONF_ECO_VALUE): cv.string,
            }
        )

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Generic Water Heater."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        
        eco_entity_args = {}
        if current.get(CONF_ECO_ENTITY):
            eco_entity_args["default"] = current.get(CONF_ECO_ENTITY)
            
        eco_value_args = {}
        if current.get(CONF_ECO_VALUE):
            eco_value_args["default"] = current.get(CONF_ECO_VALUE)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=current.get(CONF_NAME)): cv.string,
                vol.Required(CONF_HEATER, default=current.get(CONF_HEATER)): selector({"entity": {"domain": ["switch", "input_boolean"]}}),
                vol.Required(CONF_SENSOR, default=current.get(CONF_SENSOR)): selector({"entity": {"domain": "sensor", "device_class": "temperature"}}),
                vol.Optional(CONF_TEMP_STEP, default=current.get(CONF_TEMP_STEP, 1.0)): vol.Coerce(float),
                vol.Optional(CONF_COLD_TOLERANCE, default=current.get(CONF_COLD_TOLERANCE, 0.0)): vol.Coerce(float),
                vol.Optional(CONF_HOT_TOLERANCE, default=current.get(CONF_HOT_TOLERANCE, 0.0)): vol.Coerce(float),
                vol.Optional(CONF_TEMP_MIN, default=current.get(CONF_TEMP_MIN, 15.0)): vol.Coerce(float),
                vol.Optional(CONF_TEMP_MAX, default=current.get(CONF_TEMP_MAX, 80.0)): vol.Coerce(float),
                vol.Optional(CONF_MIN_ON_DURATION, default=current.get(CONF_MIN_ON_DURATION, current.get("min_cycle_duration", {"seconds": 0}))): selector({"duration": {}}),
                vol.Optional(CONF_MIN_OFF_DURATION, default=current.get(CONF_MIN_OFF_DURATION, current.get("min_cycle_duration", {"seconds": 120}))): selector({"duration": {}}),
                vol.Optional(CONF_ECO_ENTITY, **eco_entity_args): selector({"entity": {"domain": ["sensor", "binary_sensor"]}}),
                vol.Optional(CONF_ECO_VALUE, **eco_value_args): cv.string,
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)
