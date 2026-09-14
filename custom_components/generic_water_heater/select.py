"""Select platform for Generic Water Heater."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_NAME
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from . import (
    CONF_ECO_TEMPLATE,
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_HEATER,
    DOMAIN,
    LEGIONELLA_MODE_OFF,
    LEGIONELLA_MODE_ON,
    LEGIONELLA_MODE_UNTIL_DISINFECTED,
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_AUTO_RESUME,
    SMART_ECO_MODE_OFF,
    SMART_ECO_MODE_UNTIL_MANUAL,
    async_resolve_heater_device,
    legionella_signal,
    smart_eco_signal,
)

_OPTION_TO_MODE = {
    "Off": SMART_ECO_MODE_OFF,
    "On until next manual control": SMART_ECO_MODE_UNTIL_MANUAL,
    "Auto Resume after Delay": SMART_ECO_MODE_AUTO_RESUME,
    "Always ON": SMART_ECO_MODE_ALWAYS_ON,
}
_MODE_TO_OPTION = {value: key for key, value in _OPTION_TO_MODE.items()}

_OPTION_TO_LEGIONELLA = {
    "Off": LEGIONELLA_MODE_OFF,
    "Until disinfected": LEGIONELLA_MODE_UNTIL_DISINFECTED,
    "On": LEGIONELLA_MODE_ON,
}
_LEGIONELLA_TO_OPTION = {
    value: key for key, value in _OPTION_TO_LEGIONELLA.items()
}


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smart Eco select for a config entry."""
    data = {**entry.data, **getattr(entry, "options", {})}
    eco_template = (data.get(CONF_ECO_TEMPLATE) or "").strip() or None

    name = data.get(CONF_NAME)
    heater_entity_id = data.get(CONF_HEATER)

    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    if runtime.get("smart_eco_mode") is None:
        runtime["smart_eco_mode"] = SMART_ECO_MODE_AUTO_RESUME
    if runtime.get("legionella_mode") is None:
        runtime["legionella_mode"] = LEGIONELLA_MODE_OFF

    device_identifiers, device_has_name = async_resolve_heater_device(
        hass, heater_entity_id
    )

    entities = []
    if eco_template is not None:
        entities.append(
            GenericWaterHeaterSmartEcoSelect(
                hass=hass,
                entry_id=entry.entry_id,
                name=name,
                runtime=runtime,
                device_identifiers=device_identifiers,
                device_has_name=device_has_name,
            )
        )

    # Gated on the risk sensor, which is what decides when a cycle is done.
    # Without it there is nothing to aim at, and installs that never opted in
    # see no new entity at all.
    if data.get(CONF_ENABLE_LEGIONELLA_SENSOR, False):
        entities.append(
            GenericWaterHeaterLegionellaSelect(
                hass=hass,
                entry_id=entry.entry_id,
                name=name,
                runtime=runtime,
                device_identifiers=device_identifiers,
                device_has_name=device_has_name,
            )
        )

    if entities:
        async_add_entities(entities)


class GenericWaterHeaterSmartEcoSelect(SelectEntity, RestoreEntity):
    """Select Smart Eco policy behavior."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Smart Eco Mode"
    _attr_options = list(_OPTION_TO_MODE.keys())

    def __init__(
        self,
        hass,
        entry_id: str,
        name: str | None,
        runtime: dict,
        device_identifiers,
        device_has_name: bool = False,
    ):
        """Initialize Smart Eco select."""
        self.hass = hass
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_smart_eco_select"

        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        if name and not device_has_name:
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


class GenericWaterHeaterLegionellaSelect(SelectEntity, RestoreEntity):
    """Choose whether this tank chases a disinfection cycle.

    "Until disinfected" is a one-shot: it runs a single cycle and clears itself,
    so nothing ever starts again without a person asking. "On" is a standing
    policy that re-runs every time the interval lapses.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Legionella Disinfection"
    _attr_icon = "mdi:virus"
    _attr_options = list(_OPTION_TO_LEGIONELLA.keys())

    def __init__(
        self,
        hass,
        entry_id: str,
        name: str | None,
        runtime: dict,
        device_identifiers,
        device_has_name: bool = False,
    ):
        """Initialize the disinfection policy select."""
        self.hass = hass
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_legionella_select"

        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        if name and not device_has_name:
            self._attr_name = f"{name} Legionella Disinfection"
            self._attr_has_entity_name = False

    @property
    def current_option(self) -> str:
        """Return the selected disinfection policy."""
        mode = self._runtime.get("legionella_mode", LEGIONELLA_MODE_OFF)
        return _LEGIONELLA_TO_OPTION.get(mode, "Off")

    @property
    def device_info(self):
        """Return device information for device registry."""
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}

        return {"identifiers": {(DOMAIN, self._entry_id)}}

    async def async_added_to_hass(self) -> None:
        """Restore the policy and subscribe to updates from the water heater."""
        await super().async_added_to_hass()

        # Both this entity and the water heater persist the policy and restore
        # it independently. The water heater is authoritative, because it also
        # owns whether a cycle is actually in flight -- so only seed from our
        # own history when it has not set up yet.
        if self._runtime.get("water_heater_entity") is None:
            if (old_state := await self.async_get_last_state()) is not None:
                if old_state.state in _OPTION_TO_LEGIONELLA:
                    self._runtime["legionella_mode"] = _OPTION_TO_LEGIONELLA[
                        old_state.state
                    ]

        self._runtime["legionella_select_entity"] = self
        self.async_on_remove(
            lambda: self._runtime.pop("legionella_select_entity", None)
        )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                legionella_signal(self._entry_id),
                self._async_handle_legionella_signal,
            )
        )

    async def async_select_option(self, option: str) -> None:
        """Handle a person choosing a disinfection policy."""
        mode = _OPTION_TO_LEGIONELLA[option]
        self._runtime["legionella_mode"] = mode

        wh_entity = self._runtime.get("water_heater_entity")
        if wh_entity is not None and hasattr(wh_entity, "async_set_legionella_mode"):
            await wh_entity.async_set_legionella_mode(
                mode, source="legionella_select"
            )

        self.schedule_update_ha_state()

    def _async_handle_legionella_signal(self, _payload) -> None:
        """Handle the water heater clearing the policy after a cycle."""
        self.schedule_update_ha_state()
