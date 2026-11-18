"""Smart Eco pause/resume and eco-condition logic for GenericWaterHeater."""
from datetime import timedelta
from homeassistant.components import persistent_notification
from homeassistant.components.water_heater import STATE_PERFORMANCE, STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.event import TrackTemplateResult, async_call_later
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.template import result_as_boolean
import homeassistant.util.dt as dt_util
from . import (
    DEFAULT_ACTIVE_STATE,
    DOMAIN,
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_AUTO_RESUME,
    SMART_ECO_MODE_OFF,
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
    SMART_ECO_MODE_UNTIL_MANUAL,
    SMART_ECO_MODES,
    smart_eco_signal,
    smart_eco_state_signal,
)
from .water_heater_const import (
    STARTUP_GRACE,
    DISINFECTION_BYPASS_HOURS,
    _LOGGER,
)


class SmartEcoMixin:
    """Smart Eco pause/resume and eco-condition logic for GenericWaterHeater."""

    def _eco_gated(self) -> bool:
        """Whether the current heat depends on the eco condition (see attribute)."""
        policy_gated = (
            self._is_smart_eco_enforcing()
            and self._eco_condition_met
            and not self._startup_grace_active
            and self._current_operation == STATE_ELECTRIC
            and not self._disinfecting
            and not self._load_shed
        )
        # Heat that exists only because of the boost: the tank is past where
        # its normal target would stop it, so it stops when surplus leaves -
        # which is when the pack would have to carry it. That holds even with
        # the policy off or paused, which is the only case this adds. It does
        # not hold for a boost carried through a start (_surplus_for_boost):
        # that one runs whatever the surplus does, so it needs a surplus that
        # is actually read.
        boost_gated = (
            self._surplus_boost_active()
            and self._surplus_active
            and self._current_temperature is not None
            and self._current_temperature
            >= self._target_temperature + self._hot_tolerance
        )
        return policy_gated or boost_gated

    async def async_set_smart_eco_mode(
        self,
        mode: str,
        source: str,
        recalculate: bool = True,
    ) -> None:
        """Set Smart Eco policy mode."""
        if mode not in SMART_ECO_MODES:
            return

        if self._smart_eco_mode == mode and self._smart_eco_pause_reason is None:
            return

        if mode == SMART_ECO_MODE_OFF_UNTIL_TARGET:
            # Remember what to come back to. Re-selecting it while already in
            # it must not overwrite that with itself.
            if self._smart_eco_mode != SMART_ECO_MODE_OFF_UNTIL_TARGET:
                self._smart_eco_previous_mode = self._smart_eco_mode
                # Decide HERE whether the OFF in front of us is the eco gate's
                # or a person's. One line later this mode is in force, the gate
                # is no longer enforcing, and the two are indistinguishable --
                # which is how a reclaim ends up energising a tank somebody
                # switched off on purpose.
                self._off_until_target_reclaim = (
                    self._current_operation == STATE_OFF
                    and self._is_smart_eco_enforcing()
                    and not self._eco_condition_met
                )
            self._smart_eco_off_until_target_since = dt_util.utcnow().isoformat()
        else:
            self._smart_eco_previous_mode = None
            self._smart_eco_off_until_target_since = None
            self._off_until_target_reclaim = False
            self._off_until_target_for_disinfection = False

        self._smart_eco_mode = mode
        self._runtime["smart_eco_mode"] = mode
        self._clear_smart_eco_pause_state()
        self._debug_log("smart eco changed: mode=%s (source=%s)", mode, source)
        self._update_smart_eco_state()
        async_dispatcher_send(self.hass, smart_eco_signal(self._device_identifier), mode)
        async_dispatcher_send(self.hass, smart_eco_state_signal(self._device_identifier), self._runtime.get("smart_eco_state"))
        select_entity = self._runtime.get("smart_eco_select_entity")
        if select_entity is not None:
            select_entity.async_write_ha_state()
        if recalculate:
            await self._async_control_heating()

    async def async_set_smart_eco_enabled(
        self,
        enabled: bool,
        source: str,
        recalculate: bool = True,
    ) -> None:
        """Backwards compatible bridge from legacy on/off Smart Eco callers."""
        mode = SMART_ECO_MODE_AUTO_RESUME if enabled else SMART_ECO_MODE_OFF
        await self.async_set_smart_eco_mode(mode, source=source, recalculate=recalculate)

    @callback
    def _async_begin_startup_grace(self, _hass) -> None:
        """Start the grace countdown once every integration has loaded."""
        self.async_on_remove(
            async_call_later(
                self.hass,
                STARTUP_GRACE.total_seconds(),
                self._async_startup_grace_expired,
            )
        )

    async def _async_startup_grace_expired(self, _now) -> None:
        """Hand the tank back to the eco gate."""
        self._startup_grace_active = False
        self._debug_log("startup grace over; eco condition now enforced")
        self._update_smart_eco_state()
        await self._async_control_heating()

    @callback
    def _has_eco_condition(self) -> bool:
        """Whether any eco input is configured (eco_condition_configured)."""
        return (
            self._eco_template is not None
            or self._surplus_entity is not None
            or self._utility_entity is not None
        )

    def _async_refresh_eco_condition(self, result=None):
        """Re-read every eco input: render the template, read the entities."""
        self._eco_template_met = self._eco_template_result(result)
        self._refresh_eco_entities()
        self._recompute_eco_condition()

    def _eco_template_result(self, result=None) -> bool:
        """The template term: False when there is none or it cannot render."""
        if self._eco_template is None:
            return False
        if result is None:
            try:
                result = self._eco_template.async_render(parse_result=False)
            except TemplateError as err:
                _LOGGER.warning("%s: eco template render failed: %s", self.name, err)
                return False
        met = result_as_boolean(result)
        self._debug_log("eco template evaluated: template_result=%s, met=%s", result, met)
        return met

    def _entity_active(self, entity_id: str | None, active_state: str) -> bool:
        """Whether an eco input entity is in its active state.

        Missing, unavailable or unknown reads inactive: an input that cannot be
        read does not allow heating. At startup the grace covers the gap.
        """
        if entity_id is None:
            return False
        state = self.hass.states.get(entity_id)
        # The unavailable/unknown check is belt and braces, not covered: it
        # only matters if someone made one of those the active state.
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        return state.state.strip().casefold() == (
            (active_state or DEFAULT_ACTIVE_STATE).strip().casefold()
        )

    def _refresh_eco_entities(self) -> None:
        """Read the surplus and utility entities, noting surplus edges."""
        # Asked BEFORE the new reading, while the boost is still active: is
        # its element drawing at the moment the surplus leaves? That draw is
        # what clears a battery-judged surplus. Read here rather than when
        # control commanded it, because a slow relay reports its ON later.
        boost_was_heating = self._surplus_boost_heating_now()
        was_surplus = self._surplus_active
        self._surplus_active = self._entity_active(self._surplus_entity, self._surplus_state)
        if self._surplus_active:
            # Seen again: nothing is carried any more (_surplus_for_boost).
            self._carried_surplus = False
        self._utility_active = self._entity_active(self._utility_entity, self._utility_state)
        if self._surplus_active != was_surplus:
            self._handle_surplus_edge(boost_was_heating)

    def _recompute_eco_condition(self) -> None:
        """Combine the terms: surplus, OR utility while it is used, OR template."""
        previous = self._eco_condition_met
        self._eco_condition_met = (
            self._surplus_active
            or (self._utility_active and self._use_utility())
            or self._eco_template_met
        )
        if previous != self._eco_condition_met:
            self._debug_log(
                "eco condition changed: %s -> %s (surplus=%s, utility=%s, use_utility=%s, template=%s)",
                previous,
                self._eco_condition_met,
                self._surplus_active,
                self._utility_active,
                self._use_utility(),
                self._eco_template_met,
            )

    async def _async_eco_template_changed(
        self,
        event: Event[EventStateChangedData] | None,
        updates: list[TrackTemplateResult],
    ) -> None:
        """Handle eco template updates."""
        update = updates.pop()
        result = update.result

        if isinstance(result, TemplateError):
            _LOGGER.warning("%s: eco template update failed: %s", self.name, result)
            self._eco_template_met = False
        else:
            self._eco_template_met = self._eco_template_result(result)
        self._recompute_eco_condition()
        await self._async_eco_inputs_changed(event)

    async def _async_eco_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a surplus or utility entity changing state."""
        self._refresh_eco_entities()
        self._recompute_eco_condition()
        await self._async_eco_inputs_changed(event)

    async def _async_eco_inputs_changed(self, event) -> None:
        """Act on a changed eco input."""
        if event:
            self.async_set_context(event.context)

        self._update_smart_eco_state()
        self.async_write_ha_state()

        # The boost acts on surplus whether or not the policy is enforcing.
        if self._is_smart_eco_enforcing() or self._surplus_boost_configured():
            await self._async_control_heating()

    def _smart_eco_logical_heating_active(self) -> bool:
        """Return whether the water heater entity is logically requesting heat."""
        if self._current_operation == STATE_PERFORMANCE:
            return True

        if self._current_operation == STATE_ELECTRIC:
            return self._electric_mode_wants_heating()

        return False

    def _is_smart_eco_enforcing(self) -> bool:
        """Return whether Smart Eco policy is currently enforcing heater control."""
        return (
            self._has_eco_condition()
            and self._smart_eco_mode in (SMART_ECO_MODE_UNTIL_MANUAL, SMART_ECO_MODE_AUTO_RESUME, SMART_ECO_MODE_ALWAYS_ON)
            and self._smart_eco_pause_reason is None
        )

    def _clear_smart_eco_pause_state(self) -> None:
        """Clear Smart Eco pause flags and timers."""
        self._smart_eco_pause_reason = None
        self._smart_eco_resume_at = None
        self._smart_eco_idle_since = None
        self._runtime["smart_eco_pause_reason"] = None
        self._runtime["smart_eco_resume_at"] = None

        if self._smart_eco_resume_timer is not None:
            self._smart_eco_resume_timer()
            self._smart_eco_resume_timer = None

        persistent_notification.async_dismiss(
            self.hass,
            self._always_on_override_notification_id,
        )

    @property
    def _always_on_override_notification_id(self) -> str:
        """Return persistent notification id for Always ON temporary overrides."""
        return f"{DOMAIN}_{self._device_identifier}_always_on_override"

    def _start_manual_override_countdown(self, action: str, source: str) -> None:
        """Start a Smart Eco temporary override countdown using configured delay."""
        self._clear_smart_eco_pause_state()
        self._smart_eco_pause_reason = "manual_off_timer"
        resume_at = dt_util.utcnow() + timedelta(hours=self._smart_eco_manual_off_resume_hours)
        self._smart_eco_resume_at = resume_at.isoformat()
        self._runtime["smart_eco_pause_reason"] = self._smart_eco_pause_reason
        self._runtime["smart_eco_resume_at"] = self._smart_eco_resume_at
        self._smart_eco_resume_timer = async_call_later(
            self.hass,
            self._smart_eco_manual_off_resume_hours * 3600,
            self._async_resume_smart_eco_from_timer,
        )
        self._debug_log(
            "smart eco paused by manual control for %sh (action=%s, source=%s)",
            self._smart_eco_manual_off_resume_hours,
            action,
            source,
        )

    def _notify_always_on_override_started(self, action: str) -> None:
        """Create a clear notification when Always ON enters temporary override."""
        persistent_notification.async_create(
            self.hass,
            (
                "Smart Eco Mode is set to Always ON. A manual switch change was detected "
                f"({action.upper()}) on the underlying heater switch. Policy enforcement is "
                f"temporarily paused and will automatically resume in {self._smart_eco_manual_off_resume_hours} hour(s)."
            ),
            title="Smart Eco Mode Temporary Override",
            notification_id=self._always_on_override_notification_id,
        )

    def _notify_always_on_override_resumed(self) -> None:
        """Notify when Always ON policy enforcement has resumed."""
        persistent_notification.async_create(
            self.hass,
            "Smart Eco Mode Always ON has resumed policy enforcement after the temporary manual override period.",
            title="Smart Eco Mode Resumed",
            notification_id=self._always_on_override_notification_id,
        )

    def _update_smart_eco_state(self) -> None:
        """Calculate and publish Smart Eco state label."""
        if self._load_shed:
            # Reported ahead of every policy state: while shed, nothing else is
            # deciding whether this heater runs.
            state = "Shed by load balancer"
        elif not self._has_eco_condition() or self._smart_eco_mode == SMART_ECO_MODE_OFF:
            state = "Off"
        elif self._smart_eco_mode == SMART_ECO_MODE_OFF_UNTIL_TARGET:
            state = "Off until the tank reaches target"
        elif self._smart_eco_pause_reason == "until_manual":
            state = "Stopped by manual control"
        elif self._smart_eco_pause_reason == "manual_off_timer":
            # Fixed for the whole pause. When it ends is smart_eco_resume_at,
            # an absolute time the frontend can count down to by itself; a
            # countdown here rewrote the state, and a recorder row, a minute.
            if self._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON:
                state = "Always ON override (Paused by manual control)"
            else:
                state = "Paused by manual control"
        elif self._smart_eco_pause_reason == "manual_on_wait_idle":
            # Names the mechanism, not just the cause. This pause has no timer:
            # it ends by itself once the tank reaches target and stays idle, and
            # a label shared with the timed pause hid that entirely.
            state = "Paused until the tank is satisfied"
        elif self._is_smart_eco_enforcing() and self._startup_grace_active and not self._eco_condition_met:
            state = "Starting up"
        elif self._is_smart_eco_enforcing() and not self._eco_condition_met:
            state = "Blocked by eco condition"
        elif self._eco_condition_met and self._smart_eco_logical_heating_active():
            state = "Heating in eco"
        else:
            state = "Idle"

        self._runtime["smart_eco_state"] = state
        async_dispatcher_send(self.hass, smart_eco_state_signal(self._device_identifier), state)

    async def _async_resume_smart_eco_from_timer(self, _now) -> None:
        """Resume Smart Eco when the manual OFF delay expires."""
        self._smart_eco_resume_timer = None
        if self._smart_eco_mode not in (SMART_ECO_MODE_AUTO_RESUME, SMART_ECO_MODE_ALWAYS_ON) or self._smart_eco_pause_reason != "manual_off_timer":
            return

        self._debug_log("smart eco resume timer elapsed; resuming policy control")
        self._clear_smart_eco_pause_state()
        if self._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON:
            self._notify_always_on_override_resumed()
        self._update_smart_eco_state()
        await self._async_control_heating()

    @callback
    def _async_check_off_until_target(self) -> None:
        """Revert OFF_UNTIL_TARGET once the demand behind it is satisfied.

        "Target reached" cannot simply mean hvac_action == idle: PERFORMANCE
        holds the element on unconditionally and never reports idle, so a
        disinfection run bypassing eco this way would never give it back. The
        condition is therefore idle AND no disinfection cycle still in flight --
        which covers both users of this mode, because a completed cycle hands
        the tank back to ELECTRIC while it is far above target, so it reads idle
        immediately afterwards.

        Bounded as well: past smart_eco_manual_off_resume_hours the mode reverts
        regardless, so a dead element or an unreachable target cannot leave eco
        disabled indefinitely -- the failure this mode was asked for to avoid.
        """
        if self._smart_eco_mode != SMART_ECO_MODE_OFF_UNTIL_TARGET:
            return

        if self._off_until_target_expired():
            return

        satisfied = (
            self.hvac_action == "idle"
            and self._current_operation in (STATE_ELECTRIC, STATE_PERFORMANCE)
            # A shed is NOT a satisfied tank. hvac_action only reads "off" when
            # the OPERATION MODE is off, and a shed deliberately leaves the mode
            # alone and forces only the switch -- so a shed reads exactly like a
            # tank that reached target. Without this, the load balancer ends the
            # bypass on the owner's behalf. (An earlier comment here claimed a
            # shed drives hvac_action to off. It does not.)
            and not self._load_shed
            # Never hand eco back while a cycle it was bypassed FOR is still
            # outstanding. PERFORMANCE pins hvac_action to heating so this
            # rarely decides anything, but a cycle running in ELECTRIC reaches it.
            and not self._disinfecting
        )
        if satisfied:
            if self._smart_eco_idle_since is None:
                self._smart_eco_idle_since = dt_util.utcnow()
                if self._smart_eco_resume_timer is not None:
                    self._smart_eco_resume_timer()
                self._smart_eco_resume_timer = async_call_later(
                    self.hass, 60, self._async_revert_off_until_target
                )
            return

        self._smart_eco_idle_since = None

    @callback
    def _off_until_target_expired(self) -> bool:
        """Revert on the time bound. Returns True if it did."""
        if self._smart_eco_off_until_target_since is None:
            return False
        started = dt_util.parse_datetime(self._smart_eco_off_until_target_since)
        if started is None:
            # Unreadable stamp: restart the clock rather than leave it unbounded.
            self._smart_eco_off_until_target_since = dt_util.utcnow().isoformat()
            return False
        bound_hours = (
            DISINFECTION_BYPASS_HOURS
            if self._off_until_target_for_disinfection
            else self._smart_eco_manual_off_resume_hours
        )
        if dt_util.utcnow() - started < timedelta(hours=bound_hours):
            return False

        _LOGGER.info(
            "%s: smart eco off-until-target expired after %s h; restoring policy",
            self.name,
            bound_hours,
        )
        persistent_notification.async_create(
            self.hass,
            (
                f"{self.name} had Smart Eco set to 'Off until target reached', "
                f"but the tank did not get there within "
                f"{bound_hours} hours. Smart Eco has "
                "been restored so it does not stay disabled. A disinfection "
                "cycle still running will continue, but only when the eco "
                "condition allows."
            ),
            title="Smart Eco restored",
            notification_id=f"{DOMAIN}_{self._device_identifier}_off_until_target",
        )
        self.hass.async_create_task(
            self._async_restore_previous_eco_mode("time bound")
        )
        return True

    async def _async_revert_off_until_target(self, _now) -> None:
        """Revert after sustained idle."""
        self._smart_eco_resume_timer = None
        if self._smart_eco_mode != SMART_ECO_MODE_OFF_UNTIL_TARGET:
            return
        if (
            self.hvac_action == "idle"
            and self._current_operation in (STATE_ELECTRIC, STATE_PERFORMANCE)
            and not self._load_shed
            and not self._disinfecting
        ):
            await self._async_restore_previous_eco_mode("target reached")
            return
        self._async_check_off_until_target()

    async def _async_restore_previous_eco_mode(self, reason: str) -> None:
        """Put back whatever Smart Eco mode was in force before the bypass."""
        previous = self._smart_eco_previous_mode or SMART_ECO_MODE_AUTO_RESUME
        self._debug_log(
            "smart eco off-until-target ended (%s) -> %s", reason, previous
        )
        await self.async_set_smart_eco_mode(previous, source="off_until_target")

    def _async_check_manual_on_resume(self) -> None:
        """Resume Smart Eco after manual ON once target has been satisfied for 60s."""
        if self._smart_eco_pause_reason != "manual_on_wait_idle" or self._smart_eco_mode != SMART_ECO_MODE_AUTO_RESUME:
            return

        now = dt_util.utcnow()
        if self.hvac_action == "idle" and self._current_operation in (STATE_ELECTRIC, STATE_PERFORMANCE):
            if self._smart_eco_idle_since is None:
                self._smart_eco_idle_since = now
                if self._smart_eco_resume_timer is not None:
                    self._smart_eco_resume_timer()
                self._smart_eco_resume_timer = async_call_later(
                    self.hass,
                    60,
                    self._async_resume_smart_eco_after_idle,
                )
                return

        self._smart_eco_idle_since = None

    async def _async_resume_smart_eco_after_idle(self, _now) -> None:
        """Resume Smart Eco after sustained idle following manual ON override."""
        self._smart_eco_resume_timer = None
        if self._smart_eco_pause_reason != "manual_on_wait_idle" or self._smart_eco_mode != SMART_ECO_MODE_AUTO_RESUME:
            return

        if self.hvac_action == "idle" and self._current_operation in (STATE_ELECTRIC, STATE_PERFORMANCE):
            self._debug_log("manual ON override satisfied; resuming Smart Eco policy")
            self._clear_smart_eco_pause_state()
            self._update_smart_eco_state()
            await self._async_control_heating()
            return

        self._async_check_manual_on_resume()

    async def _async_pause_smart_eco_for_manual_override(self, action: str, source: str) -> None:
        """Pause or stop Smart Eco according to current policy mode and override action."""
        if (
            self._smart_eco_mode in (SMART_ECO_MODE_OFF, SMART_ECO_MODE_OFF_UNTIL_TARGET)
            or not self._has_eco_condition()
        ):
            # Nothing to pause: eco is already standing down. OFF_UNTIL_TARGET
            # additionally must not be converted into a timed pause, or it
            # would lose the mode it is supposed to revert to.
            return

        if self._smart_eco_mode == SMART_ECO_MODE_ALWAYS_ON:
            if source != "manual_switch":
                self._debug_log("manual override ignored by Always ON policy (source=%s)", source)
                return

            self._start_manual_override_countdown(action, source)
            self._notify_always_on_override_started(action)
            self._update_smart_eco_state()
            async_dispatcher_send(self.hass, smart_eco_signal(self._device_identifier), self._smart_eco_mode)
            return

        if self._smart_eco_mode == SMART_ECO_MODE_UNTIL_MANUAL:
            self._smart_eco_pause_reason = "until_manual"
            self._runtime["smart_eco_pause_reason"] = self._smart_eco_pause_reason
            self._runtime["smart_eco_resume_at"] = None
            self._debug_log("smart eco stopped by manual control (source=%s)", source)
            self._update_smart_eco_state()
            async_dispatcher_send(self.hass, smart_eco_signal(self._device_identifier), self._smart_eco_mode)
            return

        # Auto-resume mode pause behavior.
        # For manual switch toggles (e.g. smart breaker), always restart the countdown
        # from now so policy resumption timing remains predictable for users.
        if action == "manual_off" or source in ("manual_switch", "operation_mode"):
            self._start_manual_override_countdown(action, source)
        else:
            self._clear_smart_eco_pause_state()
            self._smart_eco_pause_reason = "manual_on_wait_idle"
            self._runtime["smart_eco_pause_reason"] = self._smart_eco_pause_reason
            self._runtime["smart_eco_resume_at"] = None
            self._debug_log("smart eco paused by manual ON until satisfied (source=%s)", source)

        self._update_smart_eco_state()
        async_dispatcher_send(self.hass, smart_eco_signal(self._device_identifier), self._smart_eco_mode)
