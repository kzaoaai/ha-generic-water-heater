"""Config flow for Generic Water Heater integration."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import selector

from . import (
    CONF_COLD_TOLERANCE,
    CONF_ENABLE_ELEMENT_FAULT,
    CONF_ENABLE_ENERGY_SPLIT,
    CONF_ENABLE_FORECAST_PREHEAT,
    CONF_ENABLE_HEAT_UP_RATE,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_MIN_KWH,
    CONF_FORECAST_PREHEAT_TARGET,
    CONF_DEBUG_LOGGING,
    CONF_ECO_TEMPLATE,
    CONF_ENABLE_HOT_WATER_IN_USE,
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_FLEET_STAGGER_SECONDS,
    CONF_HEATER,
    CONF_LEGIONELLA_INTERVAL_DAYS,
        CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
    CONF_HOT_TOLERANCE,
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_SENSOR,
    CONF_SURPLUS_BOOST_TARGET,
    CONF_SURPLUS_ENTITY,
    CONF_SURPLUS_POWER_ENTITY,
    CONF_SURPLUS_STATE,
    CONF_TEMP_MAX,
    CONF_UTILITY_ENTITY,
    CONF_UTILITY_STATE,
    CONF_WATER_IN_USE_ENTITY,
    CONF_POWER_SENSOR,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    DEFAULT_ACTIVE_STATE,
    DOMAIN,
    LEGACY_CONF_ECO_ENTITY,
    LEGACY_CONF_ECO_VALUE,
)
from .fleet import DEFAULT_STAGGER_SECONDS

# What can say "surplus" or "the utility carries the house": an on/off entity,
# or an enum sensor matched against its active state.
_ECO_INPUT_DOMAINS = ["binary_sensor", "input_boolean", "switch", "sensor", "input_select"]


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
    "smart_eco": (
        CONF_SURPLUS_ENTITY,
        CONF_SURPLUS_STATE,
        CONF_SURPLUS_POWER_ENTITY,
        CONF_UTILITY_ENTITY,
        CONF_UTILITY_STATE,
        CONF_ECO_TEMPLATE,
        CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
    ),
    "surplus_boost": (CONF_SURPLUS_BOOST_TARGET,),
    "fleet": (CONF_FLEET_STAGGER_SECONDS,),
    "legionella": (
        CONF_ENABLE_LEGIONELLA_SENSOR,
        CONF_LEGIONELLA_INTERVAL_DAYS,
        CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    ),
    "hot_water_in_use": (CONF_ENABLE_HOT_WATER_IN_USE, CONF_WATER_IN_USE_ENTITY),
    "metering": (CONF_POWER_SENSOR,),
    "insights": (
        CONF_ENABLE_ELEMENT_FAULT,
        CONF_ENABLE_HEAT_UP_RATE,
        CONF_ENABLE_ENERGY_SPLIT,
    ),
    "forecast_preheat": (
        CONF_ENABLE_FORECAST_PREHEAT,
        CONF_FORECAST_ENTITY,
        CONF_FORECAST_MIN_KWH,
        CONF_FORECAST_PREHEAT_TARGET,
    ),
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

            # The two entity inputs follow the water-in-use pattern: no schema
            # default (an entity selector rejects ""), only a suggested value,
            # cleared to "" by _apply_cleared_and_defaults.
            vol.Required("smart_eco"): _section("smart_eco", {
                vol.Optional(
                    CONF_SURPLUS_ENTITY,
                    description={"suggested_value": current.get(CONF_SURPLUS_ENTITY) or None},
                ): selector({"entity": {"domain": _ECO_INPUT_DOMAINS}}),
                vol.Optional(
                    CONF_SURPLUS_STATE,
                    default=current.get(CONF_SURPLUS_STATE) or DEFAULT_ACTIVE_STATE,
                ): selector({"text": {}}),
                vol.Optional(
                    CONF_SURPLUS_POWER_ENTITY,
                    description={
                        "suggested_value": current.get(CONF_SURPLUS_POWER_ENTITY) or None
                    },
                ): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
                vol.Optional(
                    CONF_UTILITY_ENTITY,
                    description={"suggested_value": current.get(CONF_UTILITY_ENTITY) or None},
                ): selector({"entity": {"domain": _ECO_INPUT_DOMAINS}}),
                vol.Optional(
                    CONF_UTILITY_STATE,
                    default=current.get(CONF_UTILITY_STATE) or DEFAULT_ACTIVE_STATE,
                ): selector({"text": {}}),
                vol.Optional(
                    CONF_ECO_TEMPLATE,
                    description={"suggested_value": _eco_template_default(current)},
                ): selector({"template": {}}),
                vol.Optional(
                    CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
                    default=current.get(CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS, 6),
                ): selector({"number": {"min": 1, "max": 48, "step": 1, "mode": "slider"}}),
            }),

            # No default: an empty boost target is "no boost", and a number
            # selector cannot hold an empty default.
            vol.Required("surplus_boost"): _section("surplus_boost", {
                vol.Optional(
                    CONF_SURPLUS_BOOST_TARGET,
                    description={"suggested_value": current.get(CONF_SURPLUS_BOOST_TARGET)},
                ): selector({"number": {"min": 0, "max": 100, "step": 0.5, "mode": "box"}}),
            }),

            vol.Required("fleet"): _section("fleet", {
                vol.Optional(
                    CONF_FLEET_STAGGER_SECONDS,
                    default=current.get(CONF_FLEET_STAGGER_SECONDS, DEFAULT_STAGGER_SECONDS),
                ): selector({"number": {"min": 0, "max": 3600, "step": 5, "mode": "box", "unit_of_measurement": "s"}}),
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

            # Like the water-in-use entity: no schema default (an entity
            # selector rejects ""), only a suggested value, and cleared to ""
            # by _apply_cleared_and_defaults.
            vol.Required("metering"): _section("metering", {
                vol.Optional(
                    CONF_POWER_SENSOR,
                    description={"suggested_value": current.get(CONF_POWER_SENSOR) or None},
                ): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
            }),

            # On by default: they only report. Element Fault and the energy
            # split also need the power sensor above.
            vol.Required("insights"): _section("insights", {
                vol.Optional(
                    CONF_ENABLE_ELEMENT_FAULT,
                    default=current.get(CONF_ENABLE_ELEMENT_FAULT, True),
                ): selector({"boolean": {}}),
                vol.Optional(
                    CONF_ENABLE_HEAT_UP_RATE,
                    default=current.get(CONF_ENABLE_HEAT_UP_RATE, True),
                ): selector({"boolean": {}}),
                vol.Optional(
                    CONF_ENABLE_ENERGY_SPLIT,
                    default=current.get(CONF_ENABLE_ENERGY_SPLIT, True),
                ): selector({"boolean": {}}),
            }),

            # Off by default: it changes what the heater does, and needs a
            # forecast entity. Like the boost target, no number defaults that
            # would read as chosen.
            vol.Required("forecast_preheat"): _section("forecast_preheat", {
                vol.Optional(
                    CONF_ENABLE_FORECAST_PREHEAT,
                    default=current.get(CONF_ENABLE_FORECAST_PREHEAT, False),
                ): selector({"boolean": {}}),
                vol.Optional(
                    CONF_FORECAST_ENTITY,
                    description={"suggested_value": current.get(CONF_FORECAST_ENTITY) or None},
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    CONF_FORECAST_MIN_KWH,
                    description={"suggested_value": current.get(CONF_FORECAST_MIN_KWH)},
                ): selector({"number": {"min": 0, "max": 1000, "step": 0.5, "mode": "box", "unit_of_measurement": "kWh"}}),
                vol.Optional(
                    CONF_FORECAST_PREHEAT_TARGET,
                    description={"suggested_value": current.get(CONF_FORECAST_PREHEAT_TARGET)},
                ): selector({"number": {"min": 0, "max": 100, "step": 0.5, "mode": "box"}}),
            }),

            vol.Required("advanced"): _section("advanced", {
                vol.Optional(
                    CONF_DEBUG_LOGGING,
                    default=current.get(CONF_DEBUG_LOGGING, False),
                ): selector({"boolean": {}}),
            }),
        }
    )


def _power_sensor_errors(hass, flat: dict) -> dict:
    """Refuse a power sensor that this integration created itself.

    The heater's own "Power (mirror)" sensor is a MIRROR of the meter named here
    (sensor.py, ElementPowerSensor). Naming a mirror as the meter would make it
    mirror itself - or another heater's mirror - and would publish a copy as the
    `power_sensor` contract instead of the meter. The selector cannot filter by
    platform, so it is checked on submit.
    """
    entity_id = (flat.get(CONF_POWER_SENSOR) or "").strip()
    if not entity_id:
        return {}
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.platform == DOMAIN:
        return {"base": "power_sensor_is_mirror"}
    return {}


def _heater_in_use_errors(hass, flat: dict, own_entry_id: str | None = None) -> dict:
    """Refuse a switch another entry already drives.

    Two heaters on one switch fight over the relay: each reads the other's
    commands as a person at the wall, and each joins the fleet as a separate
    element. Nothing else stops it, since entries are keyed by their own id.
    """
    heater = flat.get(CONF_HEATER)
    if not heater:
        return {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == own_entry_id:
            continue
        if {**entry.data, **entry.options}.get(CONF_HEATER) == heater:
            return {"base": "heater_already_configured"}
    return {}


def _surplus_boost_errors(flat: dict) -> dict:
    """Refuse a boost the heater could never act on.

    Without a surplus entity there is nothing to boost on, and it would
    otherwise be accepted and do nothing, silently.

    Deliberately NOT bounded by max_temp (4.5.1). That is the range of the
    target dial a person sets every day; the boost target is set once by the
    owner, and tying the two forced the dial range up just to admit a boost.
    The number selector bounds it to 0-100.
    """
    boost = flat.get(CONF_SURPLUS_BOOST_TARGET)
    if boost in (None, ""):
        return {}
    if not (flat.get(CONF_SURPLUS_ENTITY) or "").strip():
        return {"base": "surplus_boost_needs_surplus_entity"}
    return {}


def _surplus_power_errors(flat: dict) -> dict:
    """Refuse a surplus power sensor with no surplus entity to qualify.

    The power only sizes heat the surplus entity has already allowed; alone it
    would be accepted and do nothing, silently.
    """
    if not (flat.get(CONF_SURPLUS_POWER_ENTITY) or "").strip():
        return {}
    if not (flat.get(CONF_SURPLUS_ENTITY) or "").strip():
        return {"base": "surplus_power_needs_surplus_entity"}
    return {}


def _forecast_preheat_errors(flat: dict) -> dict:
    """Refuse a pre-heat that could never act, as for the boost."""
    if not flat.get(CONF_ENABLE_FORECAST_PREHEAT):
        return {}
    if not (flat.get(CONF_SURPLUS_ENTITY) or "").strip():
        return {"base": "surplus_boost_needs_surplus_entity"}
    if (
        not (flat.get(CONF_FORECAST_ENTITY) or "").strip()
        or flat.get(CONF_FORECAST_MIN_KWH) in (None, "")
        or flat.get(CONF_FORECAST_PREHEAT_TARGET) in (None, "")
    ):
        return {"base": "forecast_preheat_incomplete"}
    return {}


def _apply_cleared_and_defaults(user_input: dict) -> dict:
    """Flatten, then persist cleared optional fields as empty rather than absent.

    A cleared field simply does not come back from the form, and entry.data is
    merged under entry.options -- so without this an emptied value would fall
    back to whatever it used to be instead of clearing.
    """
    flat = flatten_sections(user_input)
    flat.setdefault(CONF_ECO_TEMPLATE, "")
    flat.setdefault(CONF_SURPLUS_ENTITY, "")
    flat.setdefault(CONF_SURPLUS_STATE, DEFAULT_ACTIVE_STATE)
    flat.setdefault(CONF_SURPLUS_POWER_ENTITY, "")
    flat.setdefault(CONF_UTILITY_ENTITY, "")
    flat.setdefault(CONF_UTILITY_STATE, DEFAULT_ACTIVE_STATE)
    flat.setdefault(CONF_SURPLUS_BOOST_TARGET, None)
    flat.setdefault(CONF_WATER_IN_USE_ENTITY, "")
    flat.setdefault(CONF_POWER_SENSOR, "")
    flat.setdefault(CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS, 6)
    flat.setdefault(CONF_DEBUG_LOGGING, False)
    flat.setdefault(CONF_FLEET_STAGGER_SECONDS, DEFAULT_STAGGER_SECONDS)
    flat.setdefault(CONF_ENABLE_LEGIONELLA_SENSOR, False)
    flat.setdefault(CONF_LEGIONELLA_INTERVAL_DAYS, 7)
    flat.setdefault(CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR, False)
    flat.setdefault(CONF_ENABLE_HOT_WATER_IN_USE, False)
    flat.setdefault(CONF_ENABLE_ELEMENT_FAULT, True)
    flat.setdefault(CONF_ENABLE_HEAT_UP_RATE, True)
    flat.setdefault(CONF_ENABLE_ENERGY_SPLIT, True)
    flat.setdefault(CONF_ENABLE_FORECAST_PREHEAT, False)
    flat.setdefault(CONF_FORECAST_ENTITY, "")
    flat.setdefault(CONF_FORECAST_MIN_KWH, None)
    flat.setdefault(CONF_FORECAST_PREHEAT_TARGET, None)
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
            errors = (
                _heater_in_use_errors(self.hass, flat)
                or _power_sensor_errors(self.hass, flat)
                or _surplus_boost_errors(flat)
                or _surplus_power_errors(flat)
                or _forecast_preheat_errors(flat)
            )
            if not errors:
                return self.async_create_entry(title=flat[CONF_NAME], data=flat)
            return self.async_show_form(
                step_id="user", data_schema=_build_data_schema(flat), errors=errors
            )

        return self.async_show_form(step_id="user", data_schema=_build_data_schema(), errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Generic Water Heater."""

    async def async_step_init(self, user_input=None):
        """Manage the integration options."""
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            flat = _apply_cleared_and_defaults(user_input)
            errors = (
                _heater_in_use_errors(self.hass, flat, self.config_entry.entry_id)
                or _power_sensor_errors(self.hass, flat)
                or _surplus_boost_errors(flat)
                or _surplus_power_errors(flat)
                or _forecast_preheat_errors(flat)
            )
            if not errors:
                return self.async_create_entry(title="", data=flat)
            return self.async_show_form(
                step_id="init",
                data_schema=_build_data_schema({**current, **flat}),
                errors=errors,
            )

        return self.async_show_form(step_id="init", data_schema=_build_data_schema(current))
