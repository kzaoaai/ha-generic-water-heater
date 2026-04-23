"""Config flow for Generic Water Heater integration."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import selector

from . import (
    CONF_COLD_TOLERANCE,
    CONF_DEBUG_LOGGING,
    CONF_ECO_TEMPLATE,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_HEATER,
    CONF_HOT_TOLERANCE,
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_SENSOR,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DOMAIN,
    LEGACY_CONF_ECO_ENTITY,
    LEGACY_CONF_ECO_VALUE,
)


def _eco_template_default(config: dict) -> str:
    """Return the current eco template or derive one from legacy settings."""
    if CONF_ECO_TEMPLATE in config:
        # Preserve explicit empty values (""/None) so we don't repopulate from
        # legacy eco_entity/eco_value fields when users clear the template.
        return config.get(CONF_ECO_TEMPLATE) or ""

    eco_entity = config.get(LEGACY_CONF_ECO_ENTITY)
    eco_value = config.get(LEGACY_CONF_ECO_VALUE)
    if not eco_entity or eco_value in (None, ""):
        return ""

    return "{{ states(%r) == %r }}" % (eco_entity, str(eco_value))


def _build_data_schema(current: dict | None = None) -> vol.Schema:
    """Build the config form schema."""
    current = current or {}

    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, "Generic Water Heater")): cv.string,
            vol.Required(CONF_HEATER, default=current.get(CONF_HEATER)): selector({"entity": {"domain": ["switch", "input_boolean"]}}),
            vol.Required(CONF_SENSOR, default=current.get(CONF_SENSOR)): selector({"entity": {"domain": "sensor", "device_class": "temperature"}}),
            vol.Optional(CONF_TEMP_STEP, default=current.get(CONF_TEMP_STEP, 1.0)): vol.Coerce(float),
            vol.Optional(CONF_COLD_TOLERANCE, default=current.get(CONF_COLD_TOLERANCE, 0.0)): vol.Coerce(float),
            vol.Optional(CONF_HOT_TOLERANCE, default=current.get(CONF_HOT_TOLERANCE, 0.0)): vol.Coerce(float),
            vol.Optional(CONF_TEMP_MIN, default=current.get(CONF_TEMP_MIN, 15.0)): vol.Coerce(float),
            vol.Optional(CONF_TEMP_MAX, default=current.get(CONF_TEMP_MAX, 80.0)): vol.Coerce(float),
            vol.Optional(
                CONF_MIN_ON_DURATION,
                default=current.get(CONF_MIN_ON_DURATION, current.get("min_cycle_duration", {"seconds": 0})),
            ): selector({"duration": {}}),
            vol.Optional(
                CONF_MIN_OFF_DURATION,
                default=current.get(CONF_MIN_OFF_DURATION, current.get("min_cycle_duration", {"seconds": 120})),
            ): selector({"duration": {}}),
            vol.Optional(
                CONF_ECO_TEMPLATE,
                description={"suggested_value": _eco_template_default(current)},
            ): selector({"template": {}}),
            vol.Optional(
                CONF_DEBUG_LOGGING,
                default=current.get(CONF_DEBUG_LOGGING, False),
            ): selector({"boolean": {}}),
            vol.Optional(
                CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
                default=current.get(CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR, False),
            ): selector({"boolean": {}}),
        }
    )


class GenericWaterHeaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Generic Water Heater."""

    VERSION = 3

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return options flow handler for the config entry (compat helper)."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            user_input.setdefault(CONF_ECO_TEMPLATE, "")
            user_input.setdefault(CONF_DEBUG_LOGGING, False)
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(step_id="user", data_schema=_build_data_schema(), errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Generic Water Heater."""

    async def async_step_init(self, user_input=None):
        """Manage the integration options."""
        if user_input is not None:
            # Explicitly persist CONF_ECO_TEMPLATE as "" when cleared so it
            # overrides any value in entry.data when both are merged later.
            user_input.setdefault(CONF_ECO_TEMPLATE, "")
            user_input.setdefault(CONF_DEBUG_LOGGING, False)
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(step_id="init", data_schema=_build_data_schema(current))
