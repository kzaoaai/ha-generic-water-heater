"""The two eco switches: whether the utility counts, and whether to boost.

Both are views onto the water heater, which owns and restores their state
(its ``eco_use_utility`` and ``surplus_boost_enabled`` attributes). They hold
no state of their own and are not RestoreEntity: two entities restoring the
same fact is the setup-order race 4.4.1 had to fix for the disinfection
policy, and a view cannot race.
"""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import (
    CONF_HEATER,
    CONF_POWER_SENSOR,
    CONF_SURPLUS_BOOST_TARGET,
    CONF_SURPLUS_ENTITY,
    CONF_UTILITY_ENTITY,
    DOMAIN,
    async_resolve_heater_device,
    eco_switches_signal,
)

# Runtime keys and their defaults. Use Utility defaults ON: that is what an
# install had before the switch existed. Boost defaults OFF: new behaviour is
# opted into, never arrived at by upgrading.
RUNTIME_USE_UTILITY = "eco_use_utility"
RUNTIME_BOOST_ENABLED = "surplus_boost_enabled"
DEFAULT_USE_UTILITY = True
DEFAULT_BOOST_ENABLED = False


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the eco switches for a config entry."""
    data = {**entry.data, **getattr(entry, "options", {})}
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    runtime.setdefault(RUNTIME_USE_UTILITY, DEFAULT_USE_UTILITY)
    runtime.setdefault(RUNTIME_BOOST_ENABLED, DEFAULT_BOOST_ENABLED)

    device_identifiers, device_has_name = async_resolve_heater_device(
        hass, data.get(CONF_HEATER), data.get(CONF_POWER_SENSOR)
    )
    common = {
        "entry_id": entry.entry_id,
        "name": data.get(CONF_NAME),
        "runtime": runtime,
        "device_identifiers": device_identifiers,
        "device_has_name": device_has_name,
    }
    # Each only when the input it switches is configured, so an install that
    # never opted in sees no new entity at all.
    entities = []
    if (data.get(CONF_UTILITY_ENTITY) or "").strip():
        entities.append(EcoUseUtilitySwitch(**common))
    if (data.get(CONF_SURPLUS_ENTITY) or "").strip() and data.get(
        CONF_SURPLUS_BOOST_TARGET
    ) not in (None, ""):
        entities.append(SurplusBoostSwitch(**common))
    if entities:
        async_add_entities(entities)


class _EcoSwitch(SwitchEntity):
    """A switch whose state lives on the water heater."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _runtime_key: str
    _default: bool
    _setter: str
    _suffix: str

    def __init__(
        self,
        entry_id: str,
        name: str | None,
        runtime: dict,
        device_identifiers,
        device_has_name: bool,
    ):
        self._entry_id = entry_id
        self._runtime = runtime
        self._device_identifiers = device_identifiers
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{self._suffix}"
        # Spell the name out unless the device can supply one. See
        # async_resolve_heater_device.
        if name and not device_has_name:
            self._attr_name = f"{name} {self._attr_name}"
            self._attr_has_entity_name = False

    @property
    def is_on(self) -> bool:
        return bool(self._runtime.get(self._runtime_key, self._default))

    @property
    def device_info(self):
        if self._device_identifiers:
            return {"identifiers": self._device_identifiers}
        return {"identifiers": {(DOMAIN, self._entry_id)}}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                eco_switches_signal(self._entry_id),
                self._async_handle_heater_signal,
            )
        )

    @callback
    def _async_handle_heater_signal(self, *_args) -> None:
        self.async_write_ha_state()

    async def _async_set(self, value: bool) -> None:
        heater = self._runtime.get("water_heater_entity")
        if heater is not None and heater.hass is not None:
            await getattr(heater, self._setter)(value)
        else:
            # Before the heater is added: it restores its own history and
            # signals back, so this only matters if it has none.
            self._runtime[self._runtime_key] = value
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class EcoUseUtilitySwitch(_EcoSwitch):
    """Whether the utility entity counts toward the eco condition."""

    _attr_name = "Eco: Use Utility"
    _attr_icon = "mdi:transmission-tower"
    _runtime_key = RUNTIME_USE_UTILITY
    _default = DEFAULT_USE_UTILITY
    _setter = "async_set_eco_use_utility"
    _suffix = "eco_use_utility"


class SurplusBoostSwitch(_EcoSwitch):
    """Whether surplus heats toward the boost target."""

    _attr_name = "Boost: Use PV Excess"
    _attr_icon = "mdi:solar-power-variant"
    _runtime_key = RUNTIME_BOOST_ENABLED
    _default = DEFAULT_BOOST_ENABLED
    _setter = "async_set_surplus_boost_enabled"
    _suffix = "surplus_boost"
