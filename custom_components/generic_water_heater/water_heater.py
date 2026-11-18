"""Support for generic water heater units."""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.water_heater import (
    ATTR_OPERATION_MODE,
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
    STATE_PERFORMANCE,
    STATE_ELECTRIC,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, CoreState, Event, EventStateChangedData, State, callback
from homeassistant.exceptions import ServiceValidationError, TemplateError
from homeassistant.helpers.event import (
    TrackTemplate,
    TrackTemplateResult,
    async_call_later,
    async_track_state_change_event,
    async_track_template_result,
)
from homeassistant.helpers import entity_platform
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers import issue_registry as ir
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.template import Template, result_as_boolean

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter
import homeassistant.util.dt as dt_util

from . import (
    celsius_system,
    CONF_ENABLE_FORECAST_PREHEAT,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_MIN_KWH,
    CONF_FORECAST_PREHEAT_TARGET,
    async_meter_identifiers,
    CONF_COLD_TOLERANCE,
    CONF_DEBUG_LOGGING,
    CONF_ECO_TEMPLATE,
    CONF_FLEET_STAGGER_SECONDS,
    CONF_HEATER,
    CONF_POWER_SENSOR,
    CONF_HOT_TOLERANCE,
    CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_SENSOR,
    CONF_SURPLUS_BOOST_TARGET,
    CONF_SURPLUS_ENTITY,
    CONF_SURPLUS_STATE,
    CONF_TARGET_TEMP,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_TEMP_STEP,
    CONF_UTILITY_ENTITY,
    CONF_UTILITY_STATE,
    DEFAULT_ACTIVE_STATE,
    DOMAIN,
    SERVICE_RELEASE,
    SERVICE_SHED,
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_AUTO_RESUME,
    SMART_ECO_MODE_OFF,
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
    SMART_ECO_MODE_UNTIL_MANUAL,
    SMART_ECO_MODES,
    async_get_fleet,
    eco_condition_configured,
    eco_switches_signal,
    smart_eco_signal,
    smart_eco_state_signal,
    legionella_signal,
    legionella_risk_signal,
    DISINFECTION_GIVE_UP_DAYS,
    LEGIONELLA_MODE_ASAP,
    LEGIONELLA_BYPASS_ECO_MODES,
    LEGIONELLA_MODE_OFF,
    LEGIONELLA_MODE_ON,
    LEGIONELLA_MODE_UNTIL_DISINFECTED,
    LEGIONELLA_MODES,
    LEGIONELLA_ONE_SHOT_MODES,
)
from .fleet import DEFAULT_STAGGER_SECONDS
from .insights import heat_rate_signal
from .switch import (
    DEFAULT_BOOST_ENABLED,
    DEFAULT_USE_UTILITY,
    RUNTIME_BOOST_ENABLED,
    RUNTIME_USE_UTILITY,
)
from .temperature_filter import finite
from .temperature_tracking import async_track_filtered_temperature
from .water_heater_const import (  # noqa: F401  (re-exported for tests)
    STARTUP_GRACE,
    OWN_ECHO_WINDOW,
    SURPLUS_BOOST_SHORT_RUN,
    SURPLUS_BOOST_LOCKOUT,
    DISINFECTION_BYPASS_HOURS,
    OUTAGE_REPAIR_AFTER,
    FAILSAFE_WARN_AFTER,
    SWITCH_UNAVAILABLE_AFTER,
    DISINFECTION_GOAL_C,
    _forecast_preheat_config,
    ISSUE_TEMPERATURE_SENSOR,
    ISSUE_SWITCH,
    ISSUE_DISINFECTION_GAVE_UP,
)
from .water_heater_disinfection import DisinfectionMixin
from .water_heater_eco import SmartEcoMixin
from .water_heater_surplus import SurplusBoostMixin
from .water_heater_switch import SwitchControlMixin

_LOGGER = logging.getLogger(__name__)


DEFAULT_NAME = "Generic Water Heater"


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up a water heater from a config entry."""
    # merge entry data and options so options override data
    data = {**entry.data, **getattr(entry, "options", {})}
    name = data.get(CONF_NAME, DEFAULT_NAME)
    heater_entity_id = data.get(CONF_HEATER)
    sensor_entity_id = data.get(CONF_SENSOR)
    target_temp = data.get(CONF_TARGET_TEMP)
    target_temp_step = data.get(CONF_TEMP_STEP)
    cold_tolerance = data.get(CONF_COLD_TOLERANCE, 0.0)
    hot_tolerance = data.get(CONF_HOT_TOLERANCE, 0.0)
    min_temp = data.get(CONF_TEMP_MIN, 15.0)
    max_temp = data.get(CONF_TEMP_MAX, 80.0)
    min_on_duration = data.get(CONF_MIN_ON_DURATION, data.get("min_cycle_duration"))
    min_off_duration = data.get(CONF_MIN_OFF_DURATION, data.get("min_cycle_duration"))
    eco_template = (data.get(CONF_ECO_TEMPLATE) or "").strip() or None
    debug_logging = data.get(CONF_DEBUG_LOGGING, False)
    manual_off_resume_hours = data.get(CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS, 6)
    fleet_stagger_seconds = data.get(CONF_FLEET_STAGGER_SECONDS, DEFAULT_STAGGER_SECONDS)
    power_sensor = (data.get(CONF_POWER_SENSOR) or "").strip() or None
    surplus_entity = (data.get(CONF_SURPLUS_ENTITY) or "").strip() or None
    utility_entity = (data.get(CONF_UTILITY_ENTITY) or "").strip() or None
    surplus_boost_target = data.get(CONF_SURPLUS_BOOST_TARGET)
    if surplus_boost_target in ("",):
        surplus_boost_target = None
    unit = hass.config.units.temperature_unit
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    if runtime.get("smart_eco_mode") is None:
        # Smart Eco defaults to auto-resume policy when an eco condition exists; otherwise Off.
        runtime["smart_eco_mode"] = (
            SMART_ECO_MODE_AUTO_RESUME if eco_condition_configured(data) else SMART_ECO_MODE_OFF
        )
    runtime.setdefault("smart_eco_pause_reason", None)
    runtime.setdefault("smart_eco_resume_at", None)
    runtime.setdefault("smart_eco_last_heating_mode", STATE_ELECTRIC)
    runtime.setdefault("smart_eco_state", "Off")
    runtime.setdefault(RUNTIME_USE_UTILITY, DEFAULT_USE_UTILITY)
    runtime.setdefault(RUNTIME_BOOST_ENABLED, DEFAULT_BOOST_ENABLED)

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_entry = registry.async_get(heater_entity_id)
    device_identifiers = None
    current_device_id = None

    if min_on_duration is not None and isinstance(min_on_duration, dict):
        min_on_duration = cv.time_period(min_on_duration)
        
    if min_off_duration is not None and isinstance(min_off_duration, dict):
        min_off_duration = cv.time_period(min_off_duration)

    if entity_entry and entity_entry.device_id:
        device_entry = device_registry.async_get(entity_entry.device_id)
        if device_entry:
            # Plus the meter device's, when separate: the meter is then listed
            # under the heater device's "Linked devices" (async_meter_identifiers).
            device_identifiers = set(device_entry.identifiers) | async_meter_identifiers(
                hass, heater_entity_id, data.get(CONF_POWER_SENSOR)
            )
            current_device_id = device_entry.id

    # Cleanup old device links for this config entry. Since 2026.9 a device
    # belongs to exactly one config entry, so dropping ours from a device is
    # removing it: async_remove_device, not update_device's
    # remove_config_entry_id, which stops working in 2027.8.
    linked_devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    for dev in linked_devices:
        if current_device_id and dev.id != current_device_id:
            device_registry.async_remove_device(dev.id)
        elif not current_device_id:
            # If no physical device is linked, ensure we don't keep links to old physical devices.
            # Check if it's the standalone device (identifier matches entry_id)
            is_standalone = any(dom == DOMAIN and ident == entry.entry_id for dom, ident in dev.identifiers)
            if not is_standalone:
                device_registry.async_remove_device(dev.id)

    entity = GenericWaterHeater(
        hass,
        name,
        heater_entity_id,
        sensor_entity_id,
        target_temp,
        target_temp_step,
        cold_tolerance,
        hot_tolerance,
        min_temp,
        max_temp,
        min_on_duration,
        min_off_duration,
        eco_template,
        debug_logging,
        unit,
        runtime,
        manual_off_resume_hours,
        config_entry_id=entry.entry_id,
        device_identifiers=device_identifiers,
        fleet_stagger_seconds=fleet_stagger_seconds,
        power_sensor=power_sensor,
        surplus_entity=surplus_entity,
        surplus_state=data.get(CONF_SURPLUS_STATE) or DEFAULT_ACTIVE_STATE,
        utility_entity=utility_entity,
        utility_state=data.get(CONF_UTILITY_STATE) or DEFAULT_ACTIVE_STATE,
        surplus_boost_target=(
            float(surplus_boost_target) if surplus_boost_target is not None else None
        ),
        forecast_preheat=_forecast_preheat_config(data),
    )
    runtime["water_heater_entity"] = entity
    async_add_entities([entity])

    # Entity services an external load balancer calls instead of
    # set_operation_mode, so shedding is never mistaken for a person.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_SHED, None, "async_load_shed")
    platform.async_register_entity_service(SERVICE_RELEASE, None, "async_load_release")
    return True


METER_LINK_STORE = f"{DOMAIN}.meter_links"


async def _async_link_meter_device(
    hass, entry_id: str, heater_device_id: str | None, power_sensor: str | None
) -> None:
    """Undo 4.3.x's "Connected via" meter link, if it is still ours.

    4.3.x linked the meter through `via_device` and recorded each link in a
    Store. Since 4.4.0 the meter is listed under "Linked devices" instead, by
    declaring its device's identifiers in the heater's device_info
    (async_meter_identifiers), so the old link is cleared here - only if it is
    unchanged since, so a `via_device` anyone else set is never touched - and
    its record dropped. The arguments beyond entry_id are unused; kept so the
    call site reads the same.
    """
    store = Store(hass, 1, METER_LINK_STORE)
    loaded: dict = await store.async_load() or {}
    if not loaded:
        # Drained, or never written: remove the file, so later setups take the
        # Store's no-file fast path instead of an executor read on every load.
        await store.async_remove()
        return
    mine = loaded.get(entry_id)
    if not mine:
        return
    device_id, via = mine.get("device"), mine.get("via")
    if device_id and via:
        device_registry = dr.async_get(hass)
        dev = device_registry.async_get(device_id)
        if dev is not None and dev.via_device_id == via:
            device_registry.async_update_device(device_id, via_device_id=None)
    links = {k: v for k, v in loaded.items() if k != entry_id}
    if links:
        await store.async_save(links)
    else:
        await store.async_remove()


@dataclass
class HeaterStoredData(ExtraStoredData):
    """The heater's own view of its state, kept whether or not it is available.

    The restore reads the last published state, and that is `unavailable`
    whenever the switch was off the network when Home Assistant saved it (every
    15 minutes, and at stop). Its attributes are then only the capability ones,
    so a restart caught mid-dropout came back OFF with Smart Eco, any pause, a
    running disinfection cycle, the eco switches and the target all lost. This
    copy is built from the entity's internal state, which a dropout does not
    touch, and is used when the published one is unusable. It also carries what
    is not published at all.
    """

    state: str | None
    attributes: dict[str, Any]
    off_until_target_for_disinfection: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "attributes": self.attributes,
            "off_until_target_for_disinfection": self.off_until_target_for_disinfection,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HeaterStoredData | None":
        state = data.get("state")
        attributes = data.get("attributes")
        if not isinstance(state, str) or not isinstance(attributes, dict):
            return None
        return cls(
            state,
            attributes,
            data.get("off_until_target_for_disinfection") is True,
        )


class GenericWaterHeater(
    SmartEcoMixin,
    DisinfectionMixin,
    SurplusBoostMixin,
    SwitchControlMixin,
    WaterHeaterEntity,
    RestoreEntity,
):
    """Representation of a generic water_heater device."""

    def __init__(
        self,
        hass,
        name,
        heater_entity_id,
        sensor_entity_id,
        target_temp,
        target_temp_step,
        cold_tolerance,
        hot_tolerance,
        min_temp,
        max_temp,
        min_on_duration,
        min_off_duration,
        eco_template,
        debug_logging,
        unit,
        runtime,
        manual_off_resume_hours,
        config_entry_id=None,
        device_identifiers=None,
        fleet_stagger_seconds=DEFAULT_STAGGER_SECONDS,
        power_sensor=None,
        surplus_entity=None,
        surplus_state=DEFAULT_ACTIVE_STATE,
        utility_entity=None,
        utility_state=DEFAULT_ACTIVE_STATE,
        surplus_boost_target=None,
        forecast_preheat=None,
    ):
        """Initialize the water_heater device."""
        self.hass = hass
        # Published, never read for control (CONF_POWER_SENSOR).
        self._power_sensor = power_sensor
        self._attr_name = name
        self.heater_entity_id = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self._attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.OPERATION_MODE | WaterHeaterEntityFeature.ON_OFF
        self._target_temperature = target_temp
        self._target_temperature_step = target_temp_step
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._min_temp = min_temp
        self._max_temp = max_temp
        # `is not None`, not truthiness: timedelta(0) is falsy, so a configured
        # 0 was silently replaced by the 120 s default -- the config said one
        # thing and the code did another.
        self._min_on_duration = (
            min_on_duration if min_on_duration is not None else timedelta(seconds=0)
        )
        self._min_off_duration = (
            min_off_duration if min_off_duration is not None else timedelta(seconds=120)
        )
        self._eco_template = Template(eco_template, hass) if eco_template else None
        # The eco condition's inputs (CONF_SURPLUS_ENTITY): each part is kept,
        # so the heater knows WHY it may heat, not only that it may.
        self._surplus_entity = surplus_entity
        self._surplus_state = surplus_state
        self._utility_entity = utility_entity
        self._utility_state = utility_state
        self._eco_template_met = False
        self._surplus_active = False
        self._utility_active = False
        self._surplus_boost_target = surplus_boost_target
        # {"entity", "min_kwh", "target"} when forecast pre-heat is on, else None.
        self._forecast_preheat = forecast_preheat
        # When the current surplus window opened, for the short-boost lockout.
        self._surplus_window_start: datetime | None = None
        # When the element last came on while the boost was active (None while
        # it is not). The short-run check measures from here, not only from
        # surplus arriving: see _handle_surplus_edge.
        self._boost_heating_since: datetime | None = None
        self._surplus_boost_locked_until: datetime | None = None
        self._surplus_boost_lockout_timer = None
        self._runtime = runtime
        self._smart_eco_mode = runtime.get("smart_eco_mode", SMART_ECO_MODE_OFF)
        self._smart_eco_pause_reason = runtime.get("smart_eco_pause_reason")
        self._smart_eco_resume_at = runtime.get("smart_eco_resume_at")
        self._smart_eco_last_heating_mode = runtime.get("smart_eco_last_heating_mode", STATE_ELECTRIC)
        self._smart_eco_manual_off_resume_hours = int(manual_off_resume_hours)
        self._smart_eco_idle_since = None
        self._smart_eco_resume_timer = None
        self._debug_logging = bool(debug_logging)
        self._eco_condition_met = False
        self._startup_grace_active = False
        self._unit_of_measurement = unit
        self._current_operation = STATE_ELECTRIC
        self._current_temperature = None
        # Set when the sensor REPORTS unavailable/unknown, cleared by the next
        # reading; while set, control holds the element off (the failsafe).
        # Not set merely because no reading has arrived yet: at setup control
        # runs before the sensor is read, and on a restart the sensor's
        # integration may load after this one.
        self._temperature_lost = False
        self._sensor_outage_timer = None
        self._switch_outage_timer = None
        self._failsafe_warn_timer = None
        # Armed when the heater loads with no reading (FIRST_READING_WITHIN).
        self._first_reading_timer = None
        self._failsafe_warned = False
        self._switch_unavailable_timer = None
        self._operation_list = [
            STATE_ELECTRIC,
            STATE_OFF,
            STATE_PERFORMANCE,
        ]
        self._attr_available = False
        self._attr_should_poll = False
        self._device_identifiers = device_identifiers
        self._last_commanded_switch_state = None
        # Commands sent but not yet reported back: (state, sent_at).
        self._commands_in_flight: list[tuple[str, datetime]] = []
        self._last_switch_change_time = None
        self._cooldown_timer = None
        self._pending_switch_state = None
        self._last_debug_hvac_action = None
        # Fleet-wide switch-on staggering. Every instance funnels through
        # _async_heater_turn_on, so that is where the fleet gets its say.
        self._entry_id = config_entry_id or heater_entity_id
        self._fleet_stagger_seconds = fleet_stagger_seconds
        self._fleet = async_get_fleet(hass)
        self._fleet_hold_reason = None
        # Load shedding is a third axis, deliberately independent of both the
        # operation mode and Smart Eco policy: an external load balancer must be
        # able to drop this element without its request being mistaken for a
        # person's, and without disturbing the policy the user chose.
        self._load_shed = False
        # device/unique id
        # prefer config_entry_id (when created via UI) otherwise fall back to heater entity id
        self._device_identifier = config_entry_id or heater_entity_id
        # Disinfection policy. The cycle is goal-seeking: it asks for
        # PERFORMANCE and waits for the risk sensor to report Low. It does not
        # outrank Smart Eco or a load shed -- both are checked ahead of the
        # operation mode, so respecting them needs no code here at all.
        self._legionella_mode = LEGIONELLA_MODE_OFF
        self._disinfecting = False
        # The goal Ready At was last told about (see _async_write_ha_state).
        self._announced_goal: float | None = None
        self._disinfection_prior_mode: str | None = None
        self._disinfection_started_at: str | None = None
        # After a standing policy gives up: no new cycle before this time.
        self._disinfection_retry_after: str | None = None
        # What OFF_UNTIL_TARGET reverts to, and when it started -- the second
        # is what bounds it, so a tank that never reaches target cannot leave
        # eco disabled for ever.
        self._smart_eco_previous_mode: str | None = None
        self._smart_eco_off_until_target_since: str | None = None
        # Whether the OFF this mode found was written by the eco gate, decided
        # at the moment the mode is chosen -- afterwards the gate is no longer
        # enforcing, so the question can no longer be asked. One-shot: it
        # un-parks once, and every OFF after that belongs to whoever wrote it.
        self._off_until_target_reclaim = False
        # Whether the bypass was taken out FOR a disinfection cycle, so that
        # ending the cycle can hand it straight back.
        self._off_until_target_for_disinfection = False
        # When the performance run a wall-switch ON promoted the tank to goes
        # back to electric (water_heater_switch.py), or None. Absolute, so a
        # restart keeps the bound instead of restarting it.
        self._manual_performance_until: datetime | None = None
        self._manual_performance_timer = None
        # expose unique_id for the entity
        try:
            self._attr_unique_id = f"{DOMAIN}_{self._device_identifier}"
        except Exception:
            pass

    @property
    def device_info(self):
        """Return device information for the device registry."""
        if self._device_identifiers:
            return {
                "identifiers": self._device_identifiers,
            }
        return {
            "identifiers": {(DOMAIN, self._device_identifier)},
            "name": self._attr_name,
        }

    @property
    def extra_restore_state_data(self) -> HeaterStoredData:
        """The internal state, for a restore whose published state is unusable."""
        attributes = dict(self.extra_state_attributes)
        attributes[ATTR_TEMPERATURE] = self._target_temperature
        return HeaterStoredData(
            self._current_operation,
            attributes,
            self._off_until_target_for_disinfection,
        )

    @callback
    def _async_write_ha_state(self) -> None:
        """Publish the state, and tell Ready At when what the tank heads for moved.

        Ready At recomputed only on tank readings and element changes, so a new
        target, a boost starting or ending or a disinfection cycle left its
        heading_for and estimate stale until the next reading, which can be many
        minutes. Every input of heating_goal() is published as state or an
        attribute, so each change of the goal comes with a write, and every
        write comes through here (this is the method core names for
        customising one; async_write_ha_state itself is final).
        """
        super()._async_write_ha_state()
        goal = self.heating_goal()
        if goal != self._announced_goal:
            self._announced_goal = goal
            async_dispatcher_send(self.hass, heat_rate_signal(self._entry_id))

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""
        return {
            "hvac_action": self.hvac_action,
            "smart_eco_mode": self._smart_eco_mode,
            "smart_eco_pause_reason": self._smart_eco_pause_reason,
            "smart_eco_resume_at": self._smart_eco_resume_at,
            "smart_eco_last_heating_mode": self._smart_eco_last_heating_mode,
            "smart_eco_state": self._runtime.get("smart_eco_state", "Off"),
            "smart_eco_condition_met": self._eco_condition_met,
            "fleet_stagger_seconds": self._fleet.stagger_seconds,
            "fleet_hold_reason": self._fleet_hold_reason,
            "load_shed": self._load_shed,
            "legionella_mode": self._legionella_mode,
            "disinfection_active": self._disinfecting,
            "disinfection_started_at": self._disinfection_started_at,
            "disinfection_retry_after": self._disinfection_retry_after,
            "disinfection_return_mode": self._disinfection_prior_mode,
            "smart_eco_previous_mode": self._smart_eco_previous_mode,
            "smart_eco_off_until_target_since": (
                self._smart_eco_off_until_target_since
            ),
            # When a performance run the wall switch asked for reverts to
            # electric, or None. A deadline, like smart_eco_resume_at.
            "manual_performance_until": (
                self._manual_performance_until.isoformat()
                if self._manual_performance_until is not None
                else None
            ),
            # The meter on this element's circuit, or None. A contract with
            # consumers outside this integration (a battery runtime estimate
            # follows the heater's live draw through it): renaming it is a
            # breaking change.
            "power_sensor": self._power_sensor,
            # True when this heat exists only because the eco condition allows
            # it, i.e. it would stop if the condition went false: Smart Eco
            # enforcing (template set, an enforcing mode, not paused), the
            # condition currently TRUE and not inside the startup grace (where a
            # false condition neither parks nor restores), plain electric
            # operation, no disinfection running, not shed. Performance, a
            # disinfection cycle, Smart Eco off or paused all heat regardless.
            # Same contract as power_sensor: a consumer leaves this heater's
            # draw out of what a battery must carry when the gating supply
            # leaves, so a false True hides real load.
            "eco_gated": self._eco_gated(),
            # The two eco switches, restored from here (switch.py).
            RUNTIME_USE_UTILITY: self._use_utility(),
            RUNTIME_BOOST_ENABLED: self._surplus_boost_enabled(),
            "surplus_boost_active": self._surplus_boost_active(),
            "surplus_boost_target": self._surplus_boost_target,
            "forecast_preheat_active": self._forecast_poor(),
            "surplus_boost_locked_until": (
                self._surplus_boost_locked_until.isoformat()
                if self._surplus_boost_locked()
                else None
            ),
        }

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported."""
        if self._current_operation == STATE_OFF:
            return "off"
        heater = self.hass.states.get(self.heater_entity_id)
        if heater and heater.state == STATE_ON:
            return "heating"
        return "idle"

    # Home Assistant 2026.11 reads temperatures through native_*; the plain
    # names are deprecated there and stop working in 2027.11. 2026.10 (the
    # oldest core supported, hacs.json) has only the plain names. Providing
    # both serves each core what it reads, and on 2026.11 a class that provides
    # the native member as well is not reported. Drop the plain three once the
    # minimum core is 2026.11 or later.

    @property
    def native_current_temperature(self):
        """Return current temperature."""
        return self._current_temperature

    @property
    def native_temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def native_target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def current_temperature(self):
        """Pre-2026.11 name of native_current_temperature."""
        return self.native_current_temperature

    @property
    def temperature_unit(self):
        """Pre-2026.11 name of native_temperature_unit."""
        return self.native_temperature_unit

    @property
    def target_temperature(self):
        """Pre-2026.11 name of native_target_temperature."""
        return self.native_target_temperature

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return self._target_temperature_step

    @property
    def current_operation(self):
        """Return current operation ie. on, off."""
        return self._current_operation

    @property
    def operation_list(self):
        """Return the list of available operation modes."""
        return self._operation_list

    @property
    def min_temp(self):
        """Return the minimum targetable temperature.

        Home Assistant's default for water heaters when the config sets none.
        """
        if self._min_temp is None:
            return TemperatureConverter.convert(DEFAULT_MIN_TEMP, UnitOfTemperature.FAHRENHEIT, self._unit_of_measurement)
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum targetable temperature.

        Home Assistant's default for water heaters when the config sets none.
        """
        if self._max_temp is None:
            return TemperatureConverter.convert(DEFAULT_MAX_TEMP, UnitOfTemperature.FAHRENHEIT, self._unit_of_measurement)
        return self._max_temp

    async def async_set_temperature(self, **kwargs):
        """Set a new target temperature, and the operation mode sent with it.

        Core's set_temperature service takes an optional operation_mode beside
        the temperature and hands both over. The mode used to be dropped
        without a word. It is a person's choice like set_operation_mode, so it
        goes through that method and gets everything that follows from one.
        """
        operation_mode = kwargs.get(ATTR_OPERATION_MODE)
        # Core checks the mode against operation_list for set_operation_mode
        # but not for this service. Refused before anything changes, so a bad
        # call does not half apply.
        if operation_mode is not None and operation_mode not in self.operation_list:
            raise ServiceValidationError(
                translation_domain="water_heater",
                translation_key="not_valid_operation_mode",
                translation_placeholders={
                    "entity_id": self.entity_id,
                    "operation_mode": operation_mode,
                    "operation_list": ", ".join(self.operation_list),
                },
            )

        self._target_temperature = kwargs.get(ATTR_TEMPERATURE)
        _LOGGER.debug("%s: async_set_temperature -> target=%s", self.name, self._target_temperature)

        # Only a mode that differs from the one the entity reports is a choice.
        # A client that sends back the mode it read is not a person choosing
        # it: through set_operation_mode it would drop the deadline of a
        # wall-switch performance run and leave that run unbounded, and lift a
        # shed the load balancer has in force.
        if operation_mode is not None and operation_mode != self._current_operation:
            # Its control pass sees the target stored above.
            await self.async_set_operation_mode(operation_mode)
            return
        await self._async_control_heating()

    async def async_set_operation_mode(self, operation_mode, *, eco_already_paused=False):
        """Set new operation mode.

        Now that an external load balancer uses the shed/release services, a
        call landing here is unambiguously a person, so it is safe to treat as
        manual intent.

        ``eco_already_paused`` is for callers that have just paused Smart Eco
        for this same action themselves (turn_on): leaving an eco-parked OFF
        is a heating boundary change, and pausing again here replaced
        turn_on's until-satisfied pause with the timed one.
        """
        # Always ON ignores an OFF -- checked first, so an OFF it is going to
        # ignore has no side effects: it used to end a running disinfection
        # cycle (and set the policy to Off) on the way to being ignored. The
        # one OFF it takes is the wall switch's: that starts the documented
        # timed Always ON override (manual_off_timer) before landing here, and
        # an override that was announced but then ignored brought the element
        # back on after min_off, making the wall OFF unreachable.
        if (
            operation_mode == STATE_OFF
            and self._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON
            and self._has_eco_condition()
            and self._smart_eco_pause_reason is None
        ):
            self._debug_log("ignoring manual OFF operation while Smart Eco mode is Always ON")
            await self._async_control_heating()
            return

        # Any mode a person sets replaces a wall-switch performance run and its
        # bound; the promotion itself re-arms the bound after this call.
        self._clear_manual_performance_bound()

        # A person changing the mode mid-cycle takes the tank back. Ending the
        # cycle here rather than only in the select is what stops two things:
        # an orphaned _disinfecting flag that never clears, and -- far worse --
        # _smart_eco_last_heating_mode left at PERFORMANCE, which the eco gate
        # would faithfully restore later as an unbounded performance run. That
        # is the exact failure this feature exists to prevent.
        if self._disinfecting and operation_mode != STATE_PERFORMANCE:
            self._end_disinfection("manual mode change", stand_down=True)

        old_mode = self._current_operation

        if operation_mode in (STATE_ELECTRIC, STATE_PERFORMANCE):
            self._clear_load_shed("set_operation_mode")
            self._smart_eco_last_heating_mode = operation_mode
            self._runtime["smart_eco_last_heating_mode"] = operation_mode

        is_heating_boundary_change = (
            operation_mode == STATE_OFF
            or (
                old_mode == STATE_OFF
                and operation_mode in (STATE_ELECTRIC, STATE_PERFORMANCE)
            )
        )

        if (
            not eco_already_paused
            and self._has_eco_condition()
            and old_mode != operation_mode
            and is_heating_boundary_change
        ):
            if self._smart_eco_mode == SMART_ECO_MODE_AUTO_RESUME:
                await self._async_pause_smart_eco_for_manual_override(
                    "manual_off" if operation_mode == STATE_OFF else "manual_on",
                    source="operation_mode",
                )
            elif (
                self._smart_eco_mode == SMART_ECO_MODE_UNTIL_MANUAL
                and self._smart_eco_pause_reason is None
            ):
                await self._async_pause_smart_eco_for_manual_override(
                    "manual_off" if operation_mode == STATE_OFF else "manual_on",
                    source="operation_mode",
                )

        self._current_operation = operation_mode
        _LOGGER.debug("%s: async_set_operation_mode -> mode=%s", self.name, self._current_operation)
        if old_mode != operation_mode:
            self._debug_log("operation mode changed: %s -> %s", old_mode, operation_mode)
        await self._async_control_heating()

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        if self._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON and self._has_eco_condition():
            self._debug_log("ignoring manual turn_on while Smart Eco mode is Always ON")
            await self._async_control_heating()
            return

        self._clear_load_shed("turn_on")
        await self._async_pause_smart_eco_for_manual_override("manual_on", source="turn_on")
        # Paused already, and on purpose the until-satisfied way. From a tank
        # the eco gate had parked at OFF (its usual state while blocked), the
        # mode change below is a heating boundary change, which paused again
        # with the timed countdown: the until-satisfied pause could not be
        # reached from the state a person most often presses ON in.
        await self.async_set_operation_mode(STATE_ELECTRIC, eco_already_paused=True)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        if self._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON and self._has_eco_condition():
            self._debug_log("ignoring manual turn_off while Smart Eco mode is Always ON")
            await self._async_control_heating()
            return

        await self._async_pause_smart_eco_for_manual_override("manual_off", source="turn_off")
        await self.async_set_operation_mode(STATE_OFF)

    @callback
    def adopt_restored_smart_eco_mode(self, mode: str) -> None:
        """Take the Smart Eco mode the select restored, when we restored none.

        Not ``async_set_smart_eco_mode``: that is a person choosing, with the
        side effects of one (clearing a pause, notifying). A restore is not.
        """
        if mode not in SMART_ECO_MODES:
            return
        self._smart_eco_mode = mode
        self._runtime["smart_eco_mode"] = mode
        if self.hass is not None:
            self._update_smart_eco_state()
            self.async_write_ha_state()
            self.hass.async_create_task(self._async_control_heating())

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # The meter link goes on the device THIS entity is registered on - the
        # page the person looks at. That is not always the heater switch's own
        # device: an install can carry a separate device sharing the switch's
        # identifiers, and 4.3.0 linked the switch's device instead (seen on
        # hardware 2026-10-08, fixed in 4.3.1).
        if self.registry_entry is not None:
            await _async_link_meter_device(
                self.hass,
                self._entry_id,
                self.registry_entry.device_id,
                self._power_sensor,
            )

        self._fleet.register(
            self._entry_id,
            self.name,
            stagger_seconds=self._fleet_stagger_seconds,
        )
        self.async_on_remove(self._async_fleet_unregister)

        self.async_on_remove(
            async_track_filtered_temperature(
                self.hass, self.sensor_entity_id, self._async_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_switch_changed
            )
        )

        if self.hass.state is not CoreState.running:
            # Only a Home Assistant start. A config-entry reload happens with
            # every source entity already loaded, so it needs no grace.
            self._startup_grace_active = True
            self.async_on_remove(
                async_at_started(self.hass, self._async_begin_startup_grace)
            )

        if self._eco_template:
            info = async_track_template_result(
                self.hass,
                [TrackTemplate(self._eco_template, None)],
                self._async_eco_template_changed,
            )
            self.async_on_remove(info.async_remove)
            info.async_refresh()

        eco_entities = [
            entity_id
            for entity_id in (self._surplus_entity, self._utility_entity)
            if entity_id is not None
        ]
        if eco_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, eco_entities, self._async_eco_entity_changed
                )
            )
        if self._forecast_preheat is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._forecast_preheat["entity"]],
                    self._async_forecast_changed,
                )
            )

        old_state = await self.async_get_last_state()
        extra = await self.async_get_last_extra_data()
        stored = HeaterStoredData.from_dict(extra.as_dict()) if extra is not None else None
        if stored is not None:
            # Published state unusable (saved mid-dropout, see HeaterStoredData):
            # restore from the entity's own copy instead. Otherwise the
            # published state stays the source, as it always has been.
            if old_state is None or old_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._debug_log(
                    "restore: last state was %s; using the stored copy (%s)",
                    None if old_state is None else old_state.state,
                    stored.state,
                )
                old_state = State(self.entity_id, stored.state, stored.attributes)
            self._off_until_target_for_disinfection = (
                stored.off_until_target_for_disinfection
            )
        if old_state is not None:
            if old_state.attributes.get(ATTR_TEMPERATURE) is not None:
                self._target_temperature = float(old_state.attributes.get(ATTR_TEMPERATURE))
            self._current_operation = old_state.state
            if self._current_operation == STATE_ON:
                self._current_operation = STATE_ELECTRIC
            if self._current_operation not in self._operation_list and self._current_operation == "eco":
                # Legacy mode restore: eco no longer exists as an operation mode.
                self._current_operation = STATE_OFF

            if self._current_operation not in self._operation_list:
                self._current_operation = STATE_OFF
            restored_mode = old_state.attributes.get("smart_eco_mode")
            if restored_mode in SMART_ECO_MODES:
                self._smart_eco_mode = restored_mode
                self._runtime["smart_eco_mode"] = restored_mode
            else:
                restored_smart_eco = old_state.attributes.get("smart_eco_enabled")
                if isinstance(restored_smart_eco, bool):
                    self._smart_eco_mode = SMART_ECO_MODE_AUTO_RESUME if restored_smart_eco else SMART_ECO_MODE_OFF
                    self._runtime["smart_eco_mode"] = self._smart_eco_mode

            restored_pause_reason = old_state.attributes.get("smart_eco_pause_reason")
            if isinstance(restored_pause_reason, str):
                self._smart_eco_pause_reason = restored_pause_reason
                self._runtime["smart_eco_pause_reason"] = restored_pause_reason

            # The two eco switches are views onto these (switch.py): restored
            # here only, so nothing else can restore them in another order.
            for key in (RUNTIME_USE_UTILITY, RUNTIME_BOOST_ENABLED):
                restored_switch = old_state.attributes.get(key)
                if isinstance(restored_switch, bool):
                    self._runtime[key] = restored_switch

            restored_resume_at = old_state.attributes.get("smart_eco_resume_at")
            if isinstance(restored_resume_at, str):
                self._smart_eco_resume_at = restored_resume_at
                self._runtime["smart_eco_resume_at"] = restored_resume_at

            restored_last_heating_mode = old_state.attributes.get("smart_eco_last_heating_mode")
            if restored_last_heating_mode in (STATE_ELECTRIC, STATE_PERFORMANCE):
                self._smart_eco_last_heating_mode = restored_last_heating_mode
                self._runtime["smart_eco_last_heating_mode"] = restored_last_heating_mode

            restored_previous_eco = old_state.attributes.get("smart_eco_previous_mode")
            if restored_previous_eco in SMART_ECO_MODES:
                self._smart_eco_previous_mode = restored_previous_eco
            if self._smart_eco_mode == SMART_ECO_MODE_OFF_UNTIL_TARGET:
                # Restore the bound, do not restart it. Restarting always
                # EXTENDS the bypass -- five hours in, a restart buys another
                # full window, and a Core upgrade or a crash loop could hold
                # Smart Eco down indefinitely one window at a time. Only fall
                # back to now when there is no stamp to restore, which is the
                # upgrade case: a tank already in this mode before the stamp
                # was persisted.
                restored_since = old_state.attributes.get(
                    "smart_eco_off_until_target_since"
                )
                self._smart_eco_off_until_target_since = (
                    restored_since
                    if isinstance(restored_since, str)
                    else dt_util.utcnow().isoformat()
                )

            restored_retry = old_state.attributes.get("disinfection_retry_after")
            if isinstance(restored_retry, str):
                self._disinfection_retry_after = restored_retry

            restored_legionella_mode = old_state.attributes.get("legionella_mode")
            if restored_legionella_mode in LEGIONELLA_MODES:
                self._legionella_mode = restored_legionella_mode
                self._runtime["legionella_mode"] = restored_legionella_mode
                # The select restores the same value from its own history. Push
                # this one at it so the two cannot disagree whichever platform
                # finishes setting up first.
                self._notify_legionella_select()

            if old_state.attributes.get("disinfection_active") is True:
                self._disinfecting = True
                restored_return = old_state.attributes.get("disinfection_return_mode")
                self._disinfection_prior_mode = (
                    restored_return
                    if restored_return in (STATE_ELECTRIC, STATE_OFF)
                    else STATE_ELECTRIC
                )
                restored_started = old_state.attributes.get("disinfection_started_at")
                self._disinfection_started_at = (
                    restored_started
                    if isinstance(restored_started, str)
                    else dt_util.utcnow().isoformat()
                )

            # A wall-switch performance run keeps its deadline across a
            # restart; restarting the clock would extend it every time.
            restored_until = old_state.attributes.get("manual_performance_until")
            if isinstance(restored_until, str):
                self._manual_performance_until = dt_util.parse_datetime(restored_until)

            # A shed is the balancer's to end, not a reload's. Before this an
            # options save, a reload or a restart during a shed came back
            # un-shed and energised the element on a supply the balancer
            # believed it had relieved; it reads `load_shed` to decide, so it
            # saw the tank as drawing again and could only shed it a second
            # time on its next pass. Restored as published, the release (or a
            # person asking for heat) ends it, exactly as before the restart.
            if old_state.attributes.get("load_shed") is True:
                self._load_shed = True
                self._debug_log("load shed: restored from before the reload/restart")

            # A short-boost lockout runs its 30 minutes whatever happens in
            # between; a restart inside it used to let the boost start at once.
            restored_lockout = old_state.attributes.get("surplus_boost_locked_until")
            locked_until = (
                dt_util.parse_datetime(restored_lockout)
                if isinstance(restored_lockout, str)
                else None
            )
            if locked_until is not None and locked_until > dt_util.utcnow():
                self._surplus_boost_locked_until = locked_until
                self._surplus_boost_lockout_timer = async_call_later(
                    self.hass,
                    (locked_until - dt_util.utcnow()).total_seconds(),
                    self._async_surplus_boost_lockout_ended,
                )

        # The policy select restores from its own history too, and since
        # 2026.x the platforms set up in either order. If we had nothing of our
        # own, take what the select already restored; if it has not run yet,
        # this flag tells it to restore and hand us the value.
        restored_policy = (
            old_state is not None
            and old_state.attributes.get("legionella_mode") in LEGIONELLA_MODES
        )
        if not restored_policy:
            self.adopt_restored_legionella_mode(
                self._runtime.get("legionella_mode", LEGIONELLA_MODE_OFF)
            )
        self._runtime["legionella_restored"] = restored_policy

        # Smart Eco mode, the same way: the heater's own history wins. Before
        # 4.6.1 the Smart Eco select restored it from its own last state as
        # well, unconditionally, so whichever platform set up last decided
        # what runtime held -- and the select could then show one mode while
        # the heater enforced another. If we restored none and the select has
        # not run yet, this
        # flag tells it to restore and hand us the value. (A select that ran
        # first has already been read: __init__ takes the mode from runtime.)
        restored_eco_mode = old_state is not None and (
            old_state.attributes.get("smart_eco_mode") in SMART_ECO_MODES
            or isinstance(old_state.attributes.get("smart_eco_enabled"), bool)
        )
        self._runtime["smart_eco_restored"] = restored_eco_mode

        # Ensure target temperature is set if not restored (e.g. new entity)
        if self._target_temperature is None:
            self._target_temperature = self.min_temp

        temp_sensor = self.hass.states.get(self.sensor_entity_id)
        if temp_sensor is not None:
            # Unavailable, unknown and anything else that is not a finite
            # number are all "no reading yet". float() used to raise on a
            # non-numeric state, which failed the entity's setup, and to accept
            # "nan", which no comparison can act on.
            startup_reading = finite(temp_sensor.state)
            if startup_reading is not None:
                self._current_temperature = startup_reading
        if self._current_temperature is None:
            # Not the failsafe yet, but not "not yet" for ever either (see
            # FIRST_READING_WITHIN). On a start the wait begins once Home
            # Assistant has finished starting; on a reload that is now.
            self.async_on_remove(
                async_at_started(self.hass, self._async_begin_first_reading_wait)
            )

        heater_switch = self.hass.states.get(self.heater_entity_id)
        if heater_switch and heater_switch.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._attr_available = True
            self._last_commanded_switch_state = heater_switch.state
            # A restart or reload loses the fleet's view of what is drawing. A
            # heater found already ON has to open its stagger window before any
            # sibling asks, or the sibling steps straight onto it.
            if heater_switch.state == STATE_ON:
                self._fleet.note_switch_on(self._entry_id, dt_util.utcnow())
            else:
                self._fleet.note_switch_off(self._entry_id)

        if self._smart_eco_pause_reason == "manual_off_timer" and self._smart_eco_resume_at:
            resume_at = dt_util.parse_datetime(self._smart_eco_resume_at)
            if resume_at is not None:
                remaining = (resume_at - dt_util.utcnow()).total_seconds()
                if remaining > 0:
                    self._smart_eco_resume_timer = async_call_later(
                        self.hass,
                        remaining,
                        self._async_resume_smart_eco_from_timer,
                    )
                else:
                    self._clear_smart_eco_pause_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                legionella_risk_signal(self._device_identifier),
                self._async_handle_legionella_risk,
            )
        )

        if (
            self._legionella_mode != LEGIONELLA_MODE_OFF
            and not celsius_system(self.hass)
        ):
            # No policy runs here (see celsius_system). One restored from
            # before that was enforced is dropped, and the reconcile below ends
            # its cycle: left alone it would hold performance until the
            # give-up bound, with nothing able to complete it.
            self._legionella_mode = LEGIONELLA_MODE_OFF
            self._runtime["legionella_mode"] = LEGIONELLA_MODE_OFF
            self._notify_legionella_select()

        # A restart restores current_operation and re-runs control at the end of
        # this method, so a stale PERFORMANCE would re-arm before anything could
        # stop it. Reconcile the two restored facts against each other first.
        if self._disinfecting and self._legionella_mode == LEGIONELLA_MODE_OFF:
            self._end_disinfection("policy was off at restart")
        elif (
            not self._disinfecting
            and self._current_operation == STATE_PERFORMANCE
            and self._manual_performance_until is not None
        ):
            # Past its deadline already: the control pass below ends it.
            self._debug_log(
                "restored a wall-switch performance run, bounded until %s",
                self._manual_performance_until.isoformat(),
            )
            self._arm_manual_performance_timer()
        elif not self._disinfecting and self._current_operation == STATE_PERFORMANCE:
            self._debug_log(
                "restored performance without an active cycle; leaving it alone"
            )

        self._async_refresh_eco_condition()
        async_dispatcher_send(self.hass, eco_switches_signal(self._entry_id))
        # Let the Smart Eco select show what was restored, whichever order.
        async_dispatcher_send(
            self.hass, smart_eco_signal(self._device_identifier), self._smart_eco_mode
        )
        self._update_smart_eco_state()
        # Evaluate first: a cycle that should be running must own the mode
        # before the control pass acts on it, or the pass commands the tank on
        # the pre-cycle mode and the switch change that follows is read against
        # a command the entity never meant to issue.
        await self._async_evaluate_disinfection()
        await self._async_control_heating()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending timers so nothing fires after the entity is gone.

        A fleet deferral deliberately leaves a retry timer armed, so an entry
        unloading mid-wait would otherwise call back into a removed entity.
        """
        for cancel_timer in (
            self._cooldown_timer,
            self._smart_eco_resume_timer,
            self._surplus_boost_lockout_timer,
            self._sensor_outage_timer,
            self._switch_outage_timer,
            self._failsafe_warn_timer,
            self._first_reading_timer,
            self._switch_unavailable_timer,
            self._manual_performance_timer,
        ):
            if cancel_timer is not None:
                cancel_timer()
        self._manual_performance_timer = None
        self._sensor_outage_timer = None
        self._switch_outage_timer = None
        self._failsafe_warn_timer = None
        self._first_reading_timer = None
        self._switch_unavailable_timer = None
        self._surplus_boost_lockout_timer = None
        self._cooldown_timer = None
        self._smart_eco_resume_timer = None
        await super().async_will_remove_from_hass()
