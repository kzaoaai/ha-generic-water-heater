"""The generic_water_heater integration."""
import logging

import voluptuous as vol

from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.const import CONF_NAME
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "generic_water_heater"

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
CONF_ECO_ENTITY = "eco_entity"
CONF_ECO_VALUE = "eco_value"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                cv.slug: vol.Schema(
                    {
                        vol.Required(CONF_HEATER): cv.entity_id,
                        vol.Required(CONF_SENSOR): cv.entity_id,
                        vol.Optional(CONF_COLD_TOLERANCE): vol.Coerce(float),
                        vol.Optional(CONF_HOT_TOLERANCE): vol.Coerce(float),
                        vol.Optional(CONF_TEMP_STEP): vol.Coerce(float),
                        vol.Optional(CONF_TEMP_MIN): vol.Coerce(float),
                        vol.Optional(CONF_TEMP_MAX): vol.Coerce(float),
                        vol.Optional(CONF_MIN_ON_DURATION): cv.time_period,
                        vol.Optional(CONF_MIN_OFF_DURATION): cv.time_period,
                        vol.Optional(CONF_ECO_ENTITY): cv.entity_id,
                        vol.Optional(CONF_ECO_VALUE): cv.string,
                    }
                )
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, hass_config):
    """Set up Generic Water Heaters from YAML (keeps backward compatibility)."""
    if DOMAIN in hass_config:
        for water_heater, conf in hass_config.get(DOMAIN).items():
            _LOGGER.debug("Setup %s.%s", DOMAIN, water_heater)

            conf[CONF_NAME] = water_heater
            hass.async_create_task(
                discovery.async_load_platform(
                    hass,
                    WATER_HEATER_DOMAIN,
                    DOMAIN,
                    [conf],
                    hass_config,
                )
            )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Generic Water Heater from a config entry."""
    # Forward the config entry to the water_heater platform
    await hass.config_entries.async_forward_entry_setups(entry, [WATER_HEATER_DOMAIN])

    # Register update listener and ensure it's removed on unload to prevent accumulation
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_forward_entry_unload(entry, WATER_HEATER_DOMAIN)
