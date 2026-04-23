"""Switch platform for Generic Water Heater."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_NAME
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from . import CONF_ECO_TEMPLATE, CONF_HEATER, DOMAIN, smart_eco_signal


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smart Eco switch for a config entry."""
    data = {**entry.data, **getattr(entry, "options", {})}
    name = data.get(CONF_NAME)
    heater_entity_id = data.get(CONF_HEATER)
    eco_template = (data.get(CONF_ECO_TEMPLATE) or "").strip() or None

    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    if runtime.get("smart_eco_enabled") is None:
        runtime["smart_eco_enabled"] = eco_template is not None

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
            GenericWaterHeaterSmartEcoSwitch(
                hass=hass,
                entry_id=entry.entry_id,
                name=name,
                runtime=runtime,
                device_identifiers=device_identifiers,
            )
        ]
    )


class GenericWaterHeaterSmartEcoSwitch(SwitchEntity, RestoreEntity):
    """Toggle Smart Eco policy for Generic Water Heater."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Smart Eco"

    def __init__(self, hass, entry_id: str, name: str | None, runtime: dict, device_identifiers):
        """Initialize Smart Eco switch."""
        self.hass = hass
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_smart_eco"

        if not device_identifiers and name:
            self._attr_name = f"{name} Smart Eco"
            self._attr_has_entity_name = False

    @property
    def is_on(self) -> bool:
        """Return Smart Eco state."""
        return bool(self._runtime.get("smart_eco_enabled", False))

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
            if old_state.state in ("on", "off") and self._runtime.get("smart_eco_enabled") is None:
                self._runtime["smart_eco_enabled"] = old_state.state == "on"

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                smart_eco_signal(self._entry_id),
                self._async_handle_smart_eco_signal,
            )
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Enable Smart Eco policy."""
        self._runtime["smart_eco_enabled"] = True
        await self._async_sync_water_heater(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable Smart Eco policy."""
        self._runtime["smart_eco_enabled"] = False
        await self._async_sync_water_heater(False)

    async def _async_sync_water_heater(self, enabled: bool) -> None:
        """Synchronize Smart Eco state with water heater entity."""
        wh_entity = self._runtime.get("water_heater_entity")

        if wh_entity is not None and hasattr(wh_entity, "async_set_smart_eco_enabled"):
            await wh_entity.async_set_smart_eco_enabled(enabled, source="smart_eco_switch")
        self.async_write_ha_state()

    def _async_handle_smart_eco_signal(self, _enabled: bool) -> None:
        """Handle dispatcher updates from the water heater entity."""
        self.async_write_ha_state()
