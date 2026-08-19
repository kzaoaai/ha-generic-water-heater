"""The generic_water_heater integration."""
import logging

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .fleet import FLEET_KEY, HeaterFleet

_LOGGER = logging.getLogger(__name__)

DOMAIN = "generic_water_heater"
PLATFORMS = [WATER_HEATER_DOMAIN, SENSOR_DOMAIN, SELECT_DOMAIN]

CONF_HEATER = "heater_switch"
CONF_SENSOR = "temperature_sensor"
CONF_TARGET_TEMP = "target_temperature"
CONF_TEMP_STEP = "target_temperature_step"
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_TEMP_MIN = "min_temp"
CONF_TEMP_MAX = "max_temp"
CONF_MIN_ON_DURATION = "min_on_duration"
CONF_MIN_OFF_DURATION = "min_off_duration"
CONF_ECO_TEMPLATE = "eco_mode_template_condition"
CONF_NOMINAL_POWER_W = "nominal_power_w"
CONF_FLEET_STAGGER_SECONDS = "fleet_stagger_seconds"
CONF_FLEET_POWER_BUDGET_W = "fleet_power_budget_w"
CONF_DEBUG_LOGGING = "enable_debug_logging"
CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR = "enable_max_temp_history_sensor"
CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS = "smart_eco_manual_off_resume_hours"

SMART_ECO_MODE_OFF = "off"
SMART_ECO_MODE_UNTIL_MANUAL = "until_manual"
SMART_ECO_MODE_AUTO_RESUME = "auto_resume"
SMART_ECO_MODE_ALWAYS_ON = "always_on"

LEGACY_CONF_ECO_ENTITY = "eco_entity"
LEGACY_CONF_ECO_VALUE = "eco_value"


def async_get_fleet(hass: HomeAssistant) -> HeaterFleet:
    """Return the shared fleet coordinator, creating it on first use.

    The fleet is stored under a reserved key beside the per-entry runtime dicts.
    Config entry ids are ULIDs, so FLEET_KEY cannot collide with one, and the
    fleet deliberately outlives the unload of any individual entry -- a sibling
    that is still running must keep seeing the watts it has committed.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    fleet = domain_data.get(FLEET_KEY)
    if fleet is None:
        fleet = HeaterFleet()
        domain_data[FLEET_KEY] = fleet
    return fleet


def smart_eco_signal(entry_id: str) -> str:
    """Return dispatcher signal name for Smart Eco updates."""
    return f"{DOMAIN}_smart_eco_{entry_id}"


def smart_eco_state_signal(entry_id: str) -> str:
    """Return dispatcher signal name for Smart Eco state updates."""
    return f"{DOMAIN}_smart_eco_state_{entry_id}"


async def async_setup(hass, hass_config):
    """Set up the integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Generic Water Heater from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    runtime = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    runtime.setdefault("smart_eco_mode", None)
    runtime.setdefault("smart_eco_pause_reason", None)
    runtime.setdefault("smart_eco_resume_at", None)
    runtime.setdefault("smart_eco_last_heating_mode", None)
    runtime.setdefault("smart_eco_state", "Off")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)

        # Hand back whatever share of the power budget this entry was holding,
        # so surviving siblings can use it immediately.
        fleet = domain_data.get(FLEET_KEY)
        if fleet is not None:
            fleet.unregister(entry.entry_id)
            if fleet.is_empty and not _has_entry_runtime(domain_data):
                domain_data.pop(FLEET_KEY, None)
    return unload_ok


def _has_entry_runtime(domain_data: dict) -> bool:
    """Return True when any per-entry runtime is still stored."""
    return any(key != FLEET_KEY for key in domain_data)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current format."""
    if entry.version >= 4:
        return True

    _LOGGER.debug("Migrating config entry %s from version %s", entry.entry_id, entry.version)

    new_data = _migrate_legacy_eco_config(entry.data)
    new_options = _migrate_legacy_eco_config(entry.options)

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        version=4,
    )
    return True


def _migrate_legacy_eco_config(config: dict) -> dict:
    """Convert legacy eco entity/value settings into a template condition."""
    updated = dict(config)

    eco_template = updated.get(CONF_ECO_TEMPLATE)
    eco_entity = updated.pop(LEGACY_CONF_ECO_ENTITY, None)
    eco_value = updated.pop(LEGACY_CONF_ECO_VALUE, None)
    updated.pop("map_turn_off_to_eco", None)

    if not eco_template and eco_entity and eco_value not in (None, ""):
        compare_value = str(eco_value or "")
        updated[CONF_ECO_TEMPLATE] = (
            "{{ states(%r) == %r }}" % (eco_entity, compare_value)
        )

    if CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR not in updated:
        updated[CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR] = False

    if CONF_DEBUG_LOGGING not in updated:
        updated[CONF_DEBUG_LOGGING] = False

    if CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS not in updated:
        updated[CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS] = 6

    return updated
