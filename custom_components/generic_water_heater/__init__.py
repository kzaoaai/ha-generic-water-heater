"""The generic_water_heater integration."""
import logging

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "generic_water_heater"
PLATFORMS = [WATER_HEATER_DOMAIN, SENSOR_DOMAIN, SWITCH_DOMAIN]

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
CONF_DEBUG_LOGGING = "enable_debug_logging"
CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR = "enable_max_temp_history_sensor"

LEGACY_CONF_ECO_ENTITY = "eco_entity"
LEGACY_CONF_ECO_VALUE = "eco_value"


def smart_eco_signal(entry_id: str) -> str:
    """Return dispatcher signal name for Smart Eco updates."""
    return f"{DOMAIN}_smart_eco_{entry_id}"


async def async_setup(hass, hass_config):
    """Set up the integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Generic Water Heater from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    runtime = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    runtime.setdefault("smart_eco_enabled", None)

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
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current format."""
    if entry.version >= 3:
        return True

    _LOGGER.debug("Migrating config entry %s from version %s", entry.entry_id, entry.version)

    new_data = _migrate_legacy_eco_config(entry.data)
    new_options = _migrate_legacy_eco_config(entry.options)

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        version=3,
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

    return updated
