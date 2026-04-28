"""Select platform for Generic Water Heater."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_NAME
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from . import (
    CONF_ECO_TEMPLATE,
    CONF_HEATER,
    DOMAIN,
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_AUTO_RESUME,
    SMART_ECO_MODE_OFF,
    SMART_ECO_MODE_UNTIL_MANUAL,
    smart_eco_signal,
)

_OPTION_TO_MODE = {
    "Off": SMART_ECO_MODE_OFF,
    "On until next manual control": SMART_ECO_MODE_UNTIL_MANUAL,
    "Auto Resume after Delay": SMART_ECO_MODE_AUTO_RESUME,
    "Always ON": SMART_ECO_MODE_ALWAYS_ON,
}
_MODE_TO_OPTION = {value: key for key, value in _OPTION_TO_MODE.items()}


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smart Eco select for a config entry."""
    data = {**entry.data, **getattr(entry, "options", {})}
    eco_template = (data.get(CONF_ECO_TEMPLATE) or "").strip() or None
    if eco_template is None:
        return

    name = data.get(CONF_NAME)
    heater_entity_id = data.get(CONF_HEATER)

    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    if runtime.get("smart_eco_mode") is None:
        runtime["smart_eco_mode"] = SMART_ECO_MODE_AUTO_RESUME

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_entry = registry.async_get(heater_entity_id)
    device_identifiers = None

    if entity_entry and entity_entry.device_id:
        device_entry = device_registry.async_get(entity_entry.device_id)
        if device_entry:
            device_identifiers = device_entry.identifiers

    async_add_entities(
        [
            GenericWaterHeaterSmartEcoSelect(
                hass=hass,
                entry_id=entry.entry_id,
                name=name,
                runtime=runtime,
                device_identifiers=device_identifiers,
            )
        ]
    )


class GenericWaterHeaterSmartEcoSelect(SelectEntity, RestoreEntity):
    """Select Smart Eco policy behavior."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Smart Eco Mode"
    _attr_options = list(_OPTION_TO_MODE.keys())

    def __init__(self, hass, entry_id: str, name: str | None, runtime: dict, device_identifiers):
        """Initialize Smart Eco select."""
        self.hass = hass
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_smart_eco_select"

        if not device_identifiers and name:
            self._attr_name = f"{name} Smart Eco Mode"
            self._attr_has_entity_name = False

    @property
    def current_option(self) -> str:
        """Return the currently selected Smart Eco mode."""
        mode = self._runtime.get("smart_eco_mode", SMART_ECO_MODE_OFF)
        return _MODE_TO_OPTION.get(mode, "Off")

    @property
    def device_info(self):
        """Return device information for device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}

        return {"identifiers": {(DOMAIN, self._entry_id)}}

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to Smart Eco updates."""
        await super().async_added_to_hass()

        if (old_state := await self.async_get_last_state()) is not None:
            if old_state.state in _OPTION_TO_MODE:
                self._runtime["smart_eco_mode"] = _OPTION_TO_MODE[old_state.state]

        self._runtime["smart_eco_select_entity"] = self
        self.async_on_remove(lambda: self._runtime.pop("smart_eco_select_entity", None))

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                smart_eco_signal(self._entry_id),
                self._async_handle_smart_eco_signal,
            )
        )

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting a Smart Eco mode."""
        mode = _OPTION_TO_MODE[option]
        self._runtime["smart_eco_mode"] = mode

        wh_entity = self._runtime.get("water_heater_entity")
        if wh_entity is not None and hasattr(wh_entity, "async_set_smart_eco_mode"):
            await wh_entity.async_set_smart_eco_mode(mode, source="smart_eco_select")

        self.schedule_update_ha_state()

    def _async_handle_smart_eco_signal(self, _payload) -> None:
        """Handle dispatcher updates from the water heater entity."""
        self.schedule_update_ha_state()
