"""Config flow for Generic Water Heater integration."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import selector

from . import (
    CONF_COLD_TOLERANCE,
    CONF_DEBUG_LOGGING,
    CONF_ECO_TEMPLATE,
    CONF_ENABLE_HOT_WATER_IN_USE,
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_FLEET_POWER_BUDGET_W,
    CONF_FLEET_STAGGER_SECONDS,
    CONF_HEATER,
    CONF_LEGIONELLA_INTERVAL_DAYS,
    CONF_NOMINAL_POWER_W,
        CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
    CONF_HOT_TOLERANCE,
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_SENSOR,
    CONF_TEMP_MAX,
    CONF_WATER_IN_USE_ENTITY,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DOMAIN,
    LEGACY_CONF_ECO_ENTITY,
    LEGACY_CONF_ECO_VALUE,
)
from .fleet import DEFAULT_BUDGET_W, DEFAULT_NOMINAL_POWER_W, DEFAULT_STAGGER_SECONDS


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


# Presentation only. Every field is stored FLAT, exactly as it always was, so
# nothing that reads entry.data / entry.options has to know sections exist --
# the flow nests for display and _flatten puts it back on the way in. Getting
# that wrong would silently drop keys on an options save, so it is tested.
SECTIONS: dict[str, tuple[str, ...]] = {
    "temperatures": (
        CONF_TEMP_STEP,
        CONF_COLD_TOLERANCE,
        CONF_HOT_TOLERANCE,
        CONF_TEMP_MIN,
        CONF_TEMP_MAX,
    ),
    "cycle_protection": (CONF_MIN_ON_DURATION, CONF_MIN_OFF_DURATION),
    "smart_eco": (CONF_ECO_TEMPLATE, CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS),
    "fleet": (
        CONF_NOMINAL_POWER_W,
        CONF_FLEET_STAGGER_SECONDS,
        CONF_FLEET_POWER_BUDGET_W,
    ),
    "legionella": (
        CONF_ENABLE_LEGIONELLA_SENSOR,
        CONF_LEGIONELLA_INTERVAL_DAYS,
        CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    ),
    "hot_water_in_use": (CONF_ENABLE_HOT_WATER_IN_USE, CONF_WATER_IN_USE_ENTITY),
    "advanced": (CONF_DEBUG_LOGGING,),
}


def flatten_sections(user_input: dict) -> dict:
    """Return submitted input with section wrappers removed.

    A section that the user never expanded still comes back populated with its
    defaults, so this is not lossy -- but a section key that is missing entirely
    must not erase the fields inside it, which is why absent sections are simply
    skipped rather than filled with blanks.
    """
    flat: dict = {}
    for key, value in user_input.items():
        if key in SECTIONS and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def _build_data_schema(current: dict | None = None) -> vol.Schema:
    """Build the config form schema, grouped into collapsible sections."""
    current = current or {}

    def _section(key: str, fields: dict) -> section:
        return section(vol.Schema(fields), {"collapsed": True})

    return vol.Schema(
        {
            # The three that define the heater stay at the top level, always
            # visible: without them there is nothing to configure.
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, "Generic Water Heater")): cv.string,
            vol.Required(CONF_HEATER, default=current.get(CONF_HEATER)): selector({"entity": {"domain": ["switch", "input_boolean"]}}),
            vol.Required(CONF_SENSOR, default=current.get(CONF_SENSOR)): selector({"entity": {"domain": "sensor", "device_class": "temperature"}}),

            vol.Required("temperatures"): _section("temperatures", {
                vol.Optional(CONF_TEMP_STEP, default=current.get(CONF_TEMP_STEP, 1.0)): vol.Coerce(float),
                vol.Optional(CONF_COLD_TOLERANCE, default=current.get(CONF_COLD_TOLERANCE, 0.0)): vol.Coerce(float),
                vol.Optional(CONF_HOT_TOLERANCE, default=current.get(CONF_HOT_TOLERANCE, 0.0)): vol.Coerce(float),
                vol.Optional(CONF_TEMP_MIN, default=current.get(CONF_TEMP_MIN, 15.0)): vol.Coerce(float),
                vol.Optional(CONF_TEMP_MAX, default=current.get(CONF_TEMP_MAX, 80.0)): vol.Coerce(float),
            }),

            vol.Required("cycle_protection"): _section("cycle_protection", {
                vol.Optional(
                    CONF_MIN_ON_DURATION,
                    default=current.get(CONF_MIN_ON_DURATION, current.get("min_cycle_duration", {"seconds": 0})),
                ): selector({"duration": {}}),
                vol.Optional(
                    CONF_MIN_OFF_DURATION,
                    default=current.get(CONF_MIN_OFF_DURATION, current.get("min_cycle_duration", {"seconds": 120})),
                ): selector({"duration": {}}),
            }),

            vol.Required("smart_eco"): _section("smart_eco", {
                vol.Optional(
                    CONF_ECO_TEMPLATE,
                    description={"suggested_value": _eco_template_default(current)},
                ): selector({"template": {}}),
                vol.Optional(
                    CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
                    default=current.get(CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS, 6),
                ): selector({"number": {"min": 1, "max": 48, "step": 1, "mode": "slider"}}),
            }),

            vol.Required("fleet"): _section("fleet", {
                vol.Optional(
                    CONF_NOMINAL_POWER_W,
                    default=current.get(CONF_NOMINAL_POWER_W, DEFAULT_NOMINAL_POWER_W),
                ): selector({"number": {"min": 0, "max": 20000, "step": 50, "mode": "box", "unit_of_measurement": "W"}}),
                vol.Optional(
                    CONF_FLEET_STAGGER_SECONDS,
                    default=current.get(CONF_FLEET_STAGGER_SECONDS, DEFAULT_STAGGER_SECONDS),
                ): selector({"number": {"min": 0, "max": 3600, "step": 5, "mode": "box", "unit_of_measurement": "s"}}),
                vol.Optional(
                    CONF_FLEET_POWER_BUDGET_W,
                    default=current.get(CONF_FLEET_POWER_BUDGET_W, DEFAULT_BUDGET_W),
                ): selector({"number": {"min": 0, "max": 100000, "step": 100, "mode": "box", "unit_of_measurement": "W"}}),
            }),

            vol.Required("legionella"): _section("legionella", {
                vol.Optional(
                    CONF_ENABLE_LEGIONELLA_SENSOR,
                    default=current.get(CONF_ENABLE_LEGIONELLA_SENSOR, False),
                ): selector({"boolean": {}}),
                vol.Optional(
                    CONF_LEGIONELLA_INTERVAL_DAYS,
                    default=current.get(CONF_LEGIONELLA_INTERVAL_DAYS, 7),
                ): selector({"number": {"min": 1, "max": 90, "step": 1, "mode": "box", "unit_of_measurement": "days"}}),
                vol.Optional(
                    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
                    default=current.get(CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR, False),
                ): selector({"boolean": {}}),
            }),

            vol.Required("hot_water_in_use"): _section("hot_water_in_use", {
                vol.Optional(
                    CONF_ENABLE_HOT_WATER_IN_USE,
                    default=current.get(CONF_ENABLE_HOT_WATER_IN_USE, False),
                ): selector({"boolean": {}}),
                vol.Optional(
                    CONF_WATER_IN_USE_ENTITY,
                    description={"suggested_value": current.get(CONF_WATER_IN_USE_ENTITY) or None},
                ): selector({"entity": {"domain": ["binary_sensor", "input_boolean", "switch"]}}),
            }),

            vol.Required("advanced"): _section("advanced", {
                vol.Optional(
                    CONF_DEBUG_LOGGING,
                    default=current.get(CONF_DEBUG_LOGGING, False),
                ): selector({"boolean": {}}),
            }),
        }
    )


def _apply_cleared_and_defaults(user_input: dict) -> dict:
    """Flatten, then persist cleared optional fields as empty rather than absent.

    A cleared field simply does not come back from the form, and entry.data is
    merged under entry.options -- so without this an emptied value would fall
    back to whatever it used to be instead of clearing.
    """
    flat = flatten_sections(user_input)
    flat.setdefault(CONF_ECO_TEMPLATE, "")
    flat.setdefault(CONF_WATER_IN_USE_ENTITY, "")
    flat.setdefault(CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS, 6)
    flat.setdefault(CONF_DEBUG_LOGGING, False)
    flat.setdefault(CONF_NOMINAL_POWER_W, DEFAULT_NOMINAL_POWER_W)
    flat.setdefault(CONF_FLEET_STAGGER_SECONDS, DEFAULT_STAGGER_SECONDS)
    flat.setdefault(CONF_FLEET_POWER_BUDGET_W, DEFAULT_BUDGET_W)
    flat.setdefault(CONF_ENABLE_LEGIONELLA_SENSOR, False)
    flat.setdefault(CONF_LEGIONELLA_INTERVAL_DAYS, 7)
    flat.setdefault(CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR, False)
    flat.setdefault(CONF_ENABLE_HOT_WATER_IN_USE, False)
    return flat


class GenericWaterHeaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Generic Water Heater."""

    VERSION = 4

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return options flow handler for the config entry (compat helper)."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            flat = _apply_cleared_and_defaults(user_input)
            return self.async_create_entry(title=flat[CONF_NAME], data=flat)

        return self.async_show_form(step_id="user", data_schema=_build_data_schema(), errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Generic Water Heater."""

    async def async_step_init(self, user_input=None):
        """Manage the integration options."""
        if user_input is not None:
            return self.async_create_entry(
                title="", data=_apply_cleared_and_defaults(user_input)
            )

        current = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(step_id="init", data_schema=_build_data_schema(current))
