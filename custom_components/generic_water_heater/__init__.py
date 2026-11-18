"""The generic_water_heater integration."""
import logging

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    MAJOR_VERSION,
    MINOR_VERSION,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from .fleet import FLEET_KEY, HeaterFleet

_LOGGER = logging.getLogger(__name__)

DOMAIN = "generic_water_heater"
PLATFORMS = [
    WATER_HEATER_DOMAIN,
    SENSOR_DOMAIN,
    SELECT_DOMAIN,
    BINARY_SENSOR_DOMAIN,
    SWITCH_DOMAIN,
]

CONF_HEATER = "heater_switch"
# Optional external "water is flowing somewhere in the house" entity. Configuring
# it is what creates the Hot Water In Use sensor; it is corroboration inside the
# detector rather than a gate, because it is whole-house and this tank is not.
CONF_WATER_IN_USE_ENTITY = "water_in_use_entity"
# What the element draws, as measured by a meter on its circuit. Not used for
# control here; published as the `power_sensor` attribute so another integration
# can find the heater's live draw from the heater, not from its own config.
CONF_POWER_SENSOR = "power_sensor"
# Creating the Hot Water In Use sensor hangs on this boolean, NOT on the entity
# above. The primary signal is the tank's own temperature fall, which needs no
# external help; the entity only unlocks the milder falls that need
# corroborating. Gating creation on the entity would deny the detector to anyone
# without a flow sensor, even though its strongest tier works fine for them.
CONF_ENABLE_HOT_WATER_IN_USE = "enable_hot_water_in_use"
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
# The eco condition's named inputs. Heating is allowed while the surplus
# entity is active, OR the utility entity is active and the "Eco: Use Utility"
# switch is on, OR the template (now an optional extra term) is true. Each
# entity counts as active when its state equals its configured active state,
# compared case-insensitively, so an enum sensor ("Utility") works as well as a
# binary one. Naming the inputs is what lets the heater know WHY it may heat,
# which the surplus boost needs and a template cannot tell it.
CONF_SURPLUS_ENTITY = "surplus_entity"
CONF_SURPLUS_STATE = "surplus_state"
CONF_UTILITY_ENTITY = "utility_entity"
CONF_UTILITY_STATE = "utility_state"
DEFAULT_ACTIVE_STATE = "on"
# Heat toward this target, instead of the normal one, while surplus is active
# and the "Boost: Use PV Excess" switch is on. Unset = no boost.
CONF_SURPLUS_BOOST_TARGET = "surplus_boost_target_temperature"
CONF_FLEET_STAGGER_SECONDS = "fleet_stagger_seconds"
CONF_DEBUG_LOGGING = "enable_debug_logging"
CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR = "enable_max_temp_history_sensor"
CONF_ENABLE_LEGIONELLA_SENSOR = "enable_legionella_sensor"
CONF_LEGIONELLA_INTERVAL_DAYS = "legionella_interval_days"
CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS = "smart_eco_manual_off_resume_hours"
# Optional extras (4.6.3). Each can be switched off from the options form;
# saving reloads the entry, no restart.
CONF_ENABLE_ELEMENT_FAULT = "enable_element_fault"
CONF_ENABLE_HEAT_UP_RATE = "enable_heat_up_rate"
CONF_ENABLE_ENERGY_SPLIT = "enable_energy_split"
CONF_ENABLE_FORECAST_PREHEAT = "enable_forecast_preheat"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FORECAST_MIN_KWH = "forecast_min_kwh"
CONF_FORECAST_PREHEAT_TARGET = "forecast_preheat_target_temperature"

SERVICE_SHED = "shed"
SERVICE_RELEASE = "release"

SMART_ECO_MODE_OFF = "off"
SMART_ECO_MODE_UNTIL_MANUAL = "until_manual"
SMART_ECO_MODE_AUTO_RESUME = "auto_resume"
SMART_ECO_MODE_ALWAYS_ON = "always_on"
# A TRANSIENT mode: eco stops gating until the demand that justified bypassing
# it is satisfied, then the previous mode comes back on its own. Behaves like
# OFF everywhere eco asks "am I enforcing" -- it is off, it just knows when to
# stop being off. Bounded by smart_eco_manual_off_resume_hours so a tank that
# never reaches target cannot leave eco disabled indefinitely, which is the
# exact failure this mode exists to prevent.
SMART_ECO_MODE_OFF_UNTIL_TARGET = "off_until_target"

SMART_ECO_MODES = (
    SMART_ECO_MODE_OFF,
    SMART_ECO_MODE_UNTIL_MANUAL,
    SMART_ECO_MODE_AUTO_RESUME,
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
)

# Disinfection policy, on two axes: WHEN (once now, or as needed each time the
# interval lapses) and WHETHER it waits for the Smart Eco condition.
#
# OFF is inert. The one-shots run a single cycle and then clear themselves, so
# nothing starts again without a person asking. The standing policies re-run
# every time the risk sensor reports the interval has lapsed.
#
# These internal values are stored state (restored attributes, diagnostics), so
# they keep their historical spellings; only the select labels describe them.
LEGIONELLA_MODE_OFF = "off"
LEGIONELLA_MODE_UNTIL_DISINFECTED = "until_disinfected"  # Once (eco)
LEGIONELLA_MODE_ASAP = "until_disinfected_asap"  # Once
LEGIONELLA_MODE_ON = "on"  # As needed (eco)
LEGIONELLA_MODE_ON_ASAP = "on_asap"  # As needed
LEGIONELLA_MODES = (
    LEGIONELLA_MODE_OFF,
    LEGIONELLA_MODE_UNTIL_DISINFECTED,
    LEGIONELLA_MODE_ASAP,
    LEGIONELLA_MODE_ON,
    LEGIONELLA_MODE_ON_ASAP,
)
# The ones that do not wait for eco: starting a cycle also puts Smart Eco into
# OFF_UNTIL_TARGET, which stands it down and gives it back when the cycle ends.
LEGIONELLA_BYPASS_ECO_MODES = (
    LEGIONELLA_MODE_ASAP,
    LEGIONELLA_MODE_ON_ASAP,
)
# The two that mean "do this now", as opposed to the standing policies.
LEGIONELLA_ONE_SHOT_MODES = (
    LEGIONELLA_MODE_UNTIL_DISINFECTED,
    LEGIONELLA_MODE_ASAP,
)

# A goal-seeking cycle needs a calendar bound, not a wall-clock one. Most of a
# hold is banked unpowered, coasting down over an evening, so a run-length cap
# would abort exactly the part that earns the credit. This bounds the whole
# request instead: if a tank cannot get there in three days it is not going to,
# and the owner should be told rather than left burning surplus indefinitely.
DISINFECTION_GIVE_UP_DAYS = 3

LEGACY_CONF_ECO_ENTITY = "eco_entity"
LEGACY_CONF_ECO_VALUE = "eco_value"


# The first core verified to link devices that share an identifier.
LINKED_DEVICES_SINCE = (2026, 9)


def async_meter_identifiers(
    hass: HomeAssistant, heater_entity_id: str, power_sensor: str | None
) -> set:
    """The meter device's identifiers, when it is not the heater switch's device.

    Added to the heater's device_info so Home Assistant lists the meter under
    the heater device's "Linked devices" (devices sharing an identifier are
    linked, 2026.9+) - the same way the switch's device is already listed.
    Declared, not written to the registry: Home Assistant resets a device's
    identifiers to what its entities declare on every reload (measured on
    2026.9.4), so a hand-written identifier would not survive, and clearing the
    meter removes the link by itself.
    """
    # Before 2026.9 an identifier cannot be on two devices: declaring the
    # meter's would make the heater's entities fail to set up (collision).
    # 2026.6 still refuses; 2026.9.4 links. 2026.7-8 untested, so not trusted.
    if (MAJOR_VERSION, MINOR_VERSION) < LINKED_DEVICES_SINCE:
        return set()
    sensor_id = (power_sensor or "").strip()
    if not sensor_id:
        return set()
    ereg, dreg = er.async_get(hass), dr.async_get(hass)
    meter = ereg.async_get(sensor_id)
    switch = ereg.async_get(heater_entity_id)
    if meter is None or not meter.device_id:
        return set()
    # A short-circuit, not a safety guard: a meter on the switch's own device
    # carries the switch's identifiers, which the heater device already has.
    if switch is not None and switch.device_id == meter.device_id:
        return set()
    # Belt and braces, not covered: our own mirror sits on the heater's own
    # device, whose identifiers it already has. The config flow refuses it too.
    if meter.platform == DOMAIN:
        return set()
    meter_device = dreg.async_get(meter.device_id)
    return set(meter_device.identifiers) if meter_device is not None else set()


def name_heater_entity(
    entity, domain: str, name: str | None, label: str, device_has_name: bool
) -> None:
    """Name one of the heater's helper entities, and suggest its entity_id.

    Shared by every platform (it was copied into each). The friendly name is
    spelled out unless the device can supply one (async_resolve_heater_device).
    The entity_id is suggested from the heater's own name -- `<name> <label>`,
    e.g. sensor.upstairs_water_heater_disinfection_due -- rather than left to
    Home Assistant, which builds it from the DEVICE name for an entity with
    has_entity_name. Devices get renamed, so an
    entity added later came out unlike its siblings, whose ids were fixed
    under the old device name. A suggestion only applies to a new registry
    entry: existing entities keep their ids.
    """
    if name and not device_has_name:
        entity._attr_name = f"{name} {label}"
        entity._attr_has_entity_name = False
    if name:
        entity.entity_id = f"{domain}.{slugify(f'{name} {label}')}"


def remove_switched_off_entities(
    hass: HomeAssistant, entry_id: str, domain: str, unique_ids: list[str]
) -> None:
    """Drop the registry entries of an extra that was switched off.

    Without this a switched-off sensor stays listed as "no longer provided"
    for good. Its entity_id is suggested from the heater's name, so switching
    it back on brings the same id back.
    """
    registry = er.async_get(hass)
    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        if entity_id is not None:
            entry = registry.async_get(entity_id)
            if entry is not None and entry.config_entry_id == entry_id:
                registry.async_remove(entity_id)


def async_resolve_heater_device(
    hass: HomeAssistant, heater_entity_id: str, power_sensor: str | None = None
):
    """Return (device identifiers, whether that device has a usable name).

    The identifiers are the heater switch device's, plus the meter device's
    when it is a different device (async_meter_identifiers).

    ``has_name`` is False when no device is linked to the heater switch, and
    also when a device IS linked but carries no name of its own. Some
    integrations register devices with ``name=None`` -- localtuya does -- and an
    entity that leaves Home Assistant to compose "<device name> <entity name>"
    then ends up with a bare, ambiguous one: two water heaters both reporting
    "Legionella Risk". Callers use this to decide whether to spell the full name
    out themselves instead of relying on the device for it.
    """
    entity_entry = er.async_get(hass).async_get(heater_entity_id)
    if entity_entry is None or not entity_entry.device_id:
        return None, False

    device_entry = dr.async_get(hass).async_get(entity_entry.device_id)
    if device_entry is None:
        return None, False

    # The meter identifiers here are belt and braces, not covered: the
    # water_heater entity declares them too, and that alone links the device.
    # Kept so every entity on the device declares the same identifiers.
    return (
        set(device_entry.identifiers)
        | async_meter_identifiers(hass, heater_entity_id, power_sensor),
        bool(device_entry.name_by_user or device_entry.name),
    )


def async_get_fleet(hass: HomeAssistant) -> HeaterFleet:
    """Return the shared fleet coordinator, creating it on first use.

    The fleet is stored under a reserved key beside the per-entry runtime dicts.
    Config entry ids are ULIDs, so FLEET_KEY cannot collide with one, and the
    fleet deliberately outlives the unload of any individual entry -- the
    reload an options save triggers must not clear a sibling's stagger window.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    fleet = domain_data.get(FLEET_KEY)
    if fleet is None:
        fleet = HeaterFleet()
        domain_data[FLEET_KEY] = fleet
    return fleet


def celsius_system(hass: HomeAssistant) -> bool:
    """Whether Home Assistant works in Celsius.

    The legionella sensor and the disinfection policy work in Celsius only.
    Readings arrive in the unit system's unit and are compared, unconverted,
    with thresholds that are in Celsius (60 C opens a hold). On a Fahrenheit
    system an hour at 61 F was recorded as a completed disinfection, so there
    the sensor stays Unknown and no policy runs.
    """
    return hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS


def eco_condition_configured(config: dict) -> bool:
    """Whether the entry has any eco condition: an entity input or the template.

    Smart Eco, its select and its state sensor exist only when this is true.
    """
    return any(
        (config.get(key) or "").strip()
        for key in (CONF_ECO_TEMPLATE, CONF_SURPLUS_ENTITY, CONF_UTILITY_ENTITY)
    )


def eco_switches_signal(entry_id: str) -> str:
    """Return dispatcher signal name for the eco/boost switch states."""
    return f"{DOMAIN}_eco_switches_{entry_id}"


def smart_eco_signal(entry_id: str) -> str:
    """Return dispatcher signal name for Smart Eco updates."""
    return f"{DOMAIN}_smart_eco_{entry_id}"


def smart_eco_state_signal(entry_id: str) -> str:
    """Return dispatcher signal name for Smart Eco state updates."""
    return f"{DOMAIN}_smart_eco_state_{entry_id}"


def legionella_signal(entry_id: str) -> str:
    """Return dispatcher signal name for disinfection policy updates."""
    return f"{DOMAIN}_legionella_{entry_id}"


def legionella_risk_signal(entry_id: str) -> str:
    """Return dispatcher signal name for Legionella risk updates."""
    return f"{DOMAIN}_legionella_risk_{entry_id}"


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

    runtime[CONFIG_SNAPSHOT_KEY] = (dict(entry.data), dict(entry.options))
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    # The temperature unit is read when the entities load (celsius_system, the
    # Heat-up Rate's unit), and changing the unit system is not a restart.
    # Without a reload the legionella sensor went on judging readings that had
    # turned to Fahrenheit against its Celsius thresholds.
    celsius = celsius_system(hass)

    @callback
    def _units_changed(_event) -> None:
        if celsius_system(hass) != celsius:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, _units_changed)
    )
    return True


CONFIG_SNAPSHOT_KEY = "config_snapshot"


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the configuration changed, and only then.

    Update listeners also fire for a title change (renaming the entry in the
    UI), and a reload is not free: it rebuilds the heater and drops what lives
    only in memory -- timers, a deferred ON, the surplus window.
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if runtime.get(CONFIG_SNAPSHOT_KEY) == (dict(entry.data), dict(entry.options)):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop this heater's repair issues with it (water_heater.py raises them)."""
    from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

    for kind in (
        "temperature_sensor_unavailable",
        "temperature_sensor_stale",
        "heater_switch_unavailable",
        "disinfection_gave_up",
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{kind}_{entry.entry_id}")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)

        # Belt-and-braces: unloading the platforms above already removed the
        # entity, whose own on-remove hook unregisters it and leaves its stagger
        # anchor behind for a reload. This repeats that with a timestamp rather
        # than without one, so the anchor survives even if the entity path did
        # not run -- an unregister with no `now` cannot stash.
        fleet = domain_data.get(FLEET_KEY)
        if fleet is not None:
            now = dt_util.utcnow()
            fleet.unregister(entry.entry_id, now)
            # Keep the fleet while it holds unexpired departed anchors: when
            # every entry reloads together they are the only record that a
            # sibling's element started seconds ago. Once they expire the
            # fleet is empty weight, but nothing sweeps it until the next
            # unload or a restart; one idle object is harmless.
            if not fleet.holds_state(now) and not _has_entry_runtime(domain_data):
                domain_data.pop(FLEET_KEY, None)
    return unload_ok


def _has_entry_runtime(domain_data: dict) -> bool:
    """Return True when any per-entry runtime is still stored."""
    return any(key != FLEET_KEY for key in domain_data)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current format."""
    # A newer entry (a downgrade) never reaches here: Home Assistant refuses an
    # entry whose version is above the flow's VERSION before migrating.
    if entry.version >= 4:
        return True

    _LOGGER.debug("Migrating config entry %s from version %s", entry.entry_id, entry.version)

    new_data = _migrate_legacy_eco_config(entry.data)
    # No defaults into options: options win over data in every reader, so a
    # default written here shadowed what the entry was created with. An entry
    # whose options form was never saved has options == {}, and its data could
    # hold enable_max_temp_history_sensor / enable_debug_logging True -- the v3
    # flow wrote both at creation -- which a False default then switched off.
    new_options = _migrate_legacy_eco_config(entry.options, defaults=False)

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        version=4,
    )
    return True


def _migrate_legacy_eco_config(config: dict, *, defaults: bool = True) -> dict:
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

    if not defaults:
        return updated

    if CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR not in updated:
        updated[CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR] = False

    if CONF_DEBUG_LOGGING not in updated:
        updated[CONF_DEBUG_LOGGING] = False

    if CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS not in updated:
        updated[CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS] = 6

    return updated
