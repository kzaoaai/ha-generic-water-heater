"""Heater switch control, outage/failsafe timers and issues for GenericWaterHeater."""
import logging
from homeassistant.components.water_heater import STATE_PERFORMANCE, STATE_ELECTRIC
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import issue_registry as ir
import homeassistant.util.dt as dt_util
from . import DOMAIN, SMART_ECO_MODE_OFF_UNTIL_TARGET
from .water_heater_const import (
    OWN_ECHO_WINDOW,
    OUTAGE_REPAIR_AFTER,
    FAILSAFE_WARN_AFTER,
    SWITCH_UNAVAILABLE_AFTER,
    ISSUE_TEMPERATURE_SENSOR,
    ISSUE_SWITCH,
    _LOGGER,
)


class SwitchControlMixin:
    """Heater switch control, outage/failsafe timers and issues for GenericWaterHeater."""

    async def async_load_shed(self) -> None:
        """Drop this heater for load shedding, without touching user intent.

        Called by an external load balancer instead of set_operation_mode, so
        the request is never mistaken for a person at the wall. Smart Eco policy
        is deliberately left exactly as it was -- no pause, no countdown, no
        notification -- so releasing the shed resumes what the user had.
        """
        if self._load_shed:
            return

        _LOGGER.debug("%s: load shed requested", self.name)
        self._debug_log("load shed: engaged (mode=%s)", self._current_operation)
        self._load_shed = True
        await self._async_control_heating()
        self.async_write_ha_state()

    async def async_load_release(self) -> None:
        """Release a load shed and resume whatever the user had configured."""
        if not self._load_shed:
            return

        _LOGGER.debug("%s: load shed released", self.name)
        self._debug_log("load shed: released (mode=%s)", self._current_operation)
        self._load_shed = False
        await self._async_control_heating()
        self.async_write_ha_state()

    @callback
    def _clear_load_shed(self, source: str) -> bool:
        """Clear a shed because a person asked for heat. Returns True if cleared.

        Only requests for heat clear a shed. A request to turn the heater off
        has nothing to win -- it is already off -- and clearing on it would let
        an ignored OFF (Smart Eco Always ON) switch the element back on.
        """
        if not self._load_shed:
            return False

        _LOGGER.debug("%s: load shed cleared by %s", self.name, source)
        self._debug_log("load shed: cleared by %s (human intent wins)", source)
        self._load_shed = False
        return True

    def _issue_id(self, kind: str) -> str:
        return f"{kind}_{self._device_identifier}"

    @callback
    def _raise_issue(self, kind: str, **placeholders: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(kind),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders={"name": str(self.name), **placeholders},
        )

    @callback
    def _clear_issue(self, kind: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(kind))

    @callback
    def _start_outage_watch(self, what: str) -> None:
        """Raise a repair if the sensor or switch is still down after a while."""
        attr = "_sensor_outage_timer" if what == "sensor" else "_switch_outage_timer"
        if getattr(self, attr) is not None or self.hass is None:
            return
        kind, entity_id = (
            (ISSUE_TEMPERATURE_SENSOR, self.sensor_entity_id)
            if what == "sensor"
            else (ISSUE_SWITCH, self.heater_entity_id)
        )

        @callback
        def _raise(_now) -> None:
            setattr(self, attr, None)
            self._raise_issue(kind, entity_id=str(entity_id))

        setattr(
            self,
            attr,
            async_call_later(self.hass, OUTAGE_REPAIR_AFTER.total_seconds(), _raise),
        )

    def _start_failsafe_warning(self) -> None:
        """Warn about the failsafe if the reading is still missing later."""
        if self._failsafe_warn_timer is not None or self.hass is None:
            return

        @callback
        def _warn(_now) -> None:
            self._failsafe_warn_timer = None
            if not self._temperature_lost:
                return
            self._failsafe_warned = True
            _LOGGER.warning(
                "No Temperature information for %d minutes, in Failsafe, "
                "holding heater %s off",
                FAILSAFE_WARN_AFTER.total_seconds() // 60,
                self.heater_entity_id,
            )

        self._failsafe_warn_timer = async_call_later(
            self.hass, FAILSAFE_WARN_AFTER.total_seconds(), _warn
        )

    def _start_switch_unavailable_timer(self) -> None:
        """Show the heater unavailable only once the switch has stayed away."""
        if (
            not self._attr_available
            or self._switch_unavailable_timer is not None
            or self.hass is None
        ):
            return

        @callback
        def _expire(_now) -> None:
            self._switch_unavailable_timer = None
            self._attr_available = False
            self.async_write_ha_state()

        self._switch_unavailable_timer = async_call_later(
            self.hass, SWITCH_UNAVAILABLE_AFTER.total_seconds(), _expire
        )

    @callback
    def _cancel_switch_unavailable_timer(self) -> None:
        if self._switch_unavailable_timer is not None:
            self._switch_unavailable_timer()
            self._switch_unavailable_timer = None

    @callback
    def _end_failsafe_warning(self) -> None:
        if self._failsafe_warn_timer is not None:
            self._failsafe_warn_timer()
            self._failsafe_warn_timer = None
        self._failsafe_warned = False

    @callback
    def _end_outage_watch(self, what: str) -> None:
        attr = "_sensor_outage_timer" if what == "sensor" else "_switch_outage_timer"
        if (timer := getattr(self, attr)) is not None:
            timer()
            setattr(self, attr, None)
        if self.hass is not None:
            self._clear_issue(
                ISSUE_TEMPERATURE_SENSOR if what == "sensor" else ISSUE_SWITCH
            )

    async def _async_sensor_changed(self, value, _when, _attributes):
        """Handle a temperature reading that survived the spike filter."""
        if value is None or value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Failsafe. The control pass at the end of this method switches
            # the element off and holds it off for as long as the reading is
            # missing (see _async_control_heating). Turning it off here alone
            # was not enough: in PERFORMANCE that same pass switched it straight
            # back on, and an OFF deferred by min_on_duration was retried
            # through a pass that skipped control for want of a temperature.
            if not self._temperature_lost:
                # Once per outage: it used to warn on every unavailable or
                # unknown report, which a flapping sensor repeats for hours.
                # And only once the outage has lasted: see FAILSAFE_WARN_AFTER.
                _LOGGER.debug(
                    "%s: no temperature information, entering Failsafe, "
                    "turning off heater %s",
                    self.name,
                    self.heater_entity_id,
                )
                self._start_failsafe_warning()
                self._start_outage_watch("sensor")
            self._current_temperature = None
            self._temperature_lost = True
        else:
            try:
                self._current_temperature = float(value)
            except (TypeError, ValueError):
                return
            if self._temperature_lost:
                # Only worth an INFO line when the entry was worth a WARNING.
                _LOGGER.log(
                    logging.INFO if self._failsafe_warned else logging.DEBUG,
                    "%s: temperature reading is back, leaving Failsafe",
                    self.name,
                )
                self._end_failsafe_warning()
                self._end_outage_watch("sensor")
            self._temperature_lost = False

        _LOGGER.debug(
            "%s: sensor changed -> current_temperature=%s, target=%s, cold_tolerance=%s, hot_tolerance=%s",
            self.name,
            self._current_temperature,
            self._target_temperature,
            self._cold_tolerance,
            self._hot_tolerance,
        )
        self._debug_log(
            "sensor update: temp_sensor_entity=%s, current_temperature=%.1f, target=%.1f, mode=%s",
            self.sensor_entity_id,
            self._current_temperature if self._current_temperature is not None else 0,
            self._target_temperature if self._target_temperature is not None else 0,
            self._current_operation,
        )

        await self._async_control_heating()

    @callback
    def _async_switch_changed(self, event):
        """Handle heater switch state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        _LOGGER.debug("New switch state = %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            if new_state is None:
                # Removed, not dropped: nothing will bring it back.
                self._cancel_switch_unavailable_timer()
                self._attr_available = False
            else:
                self._start_switch_unavailable_timer()
            self._start_outage_watch("switch")
        else:
            self._cancel_switch_unavailable_timer()
            self._attr_available = True
            self._end_outage_watch("switch")
            _LOGGER.debug("%s became Available", self.name)
            self._debug_log(
                "switch changed: new_state=%s, last_commanded=%s, mode=%s",
                new_state.state,
                self._last_commanded_switch_state,
                self._current_operation,
            )

            now = dt_util.utcnow()
            state_changed = old_state is not None and old_state.state != new_state.state
            # A device coming back from unavailable is reporting the state it
            # already had -- see the override chain below.
            returned_from_unavailable = old_state is None or old_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            )
            # Only a real flip restarts the min on/off clock. An attribute-only
            # update (rssi, power) arrives here too, and stamping it pushed a
            # held OFF back by the full min_on_duration on every update: a
            # switch that reports attributes faster than that never turned off.
            # A reappearance is not a flip either; stamping it would delay the
            # reconcile pass it triggers below.
            if state_changed and not returned_from_unavailable:
                self._last_switch_change_time = now
            # Manual and externally driven flips energise the same inverter
            # whether or not the fleet ever admitted them.
            if new_state.state == STATE_ON:
                self._fleet.note_switch_on(self._entry_id, now)
            else:
                self._fleet.note_switch_off(self._entry_id)
            had_pending = self._pending_switch_state is not None

            # If we have no record of what we last commanded, seed from old_state so we
            # can still detect the current manual flip as an override.
            if self._last_commanded_switch_state is None and old_state is not None and old_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._last_commanded_switch_state = old_state.state

            # A real switch state change supersedes any delayed action that was queued.
            if state_changed and self._cooldown_timer is not None:
                self._cooldown_timer()
                self._cooldown_timer = None
                self._debug_log(
                    "cooldown timer canceled due to switch state change: %s -> %s",
                    old_state.state,
                    new_state.state,
                )

            # A device coming back from unavailable is reporting the state it
            # already had -- it is not a person acting on the switch. Treating
            # it as one let a Wi-Fi blip promote ELECTRIC to PERFORMANCE (the
            # "they flipped it on even though electric would idle, so they want
            # heat now" rule) and run a multi-kW element to the tank's
            # mechanical cutout. Observed 2026-09-06 and 2026-09-08: eleven
            # dropouts, two runaways to 63.7 C and 64.4 C against a 45 C target.
            #
            # This is the same exclusion the baseline-seeding line above already
            # applies; it simply was never applied to the override branches.
            # A genuine flip still arrives as a real off -> on transition.
            # (returned_from_unavailable is computed at the top of this branch.)

            if self._startup_grace_active and state_changed:
                # A device reconnecting after a restart can replay states: on
                # 2026-10-02 a relay reported on then off within 250 ms, with no
                # command behind either, and the ON started a 3-hour manual eco
                # pause. Nothing during startup can be told apart from that, so
                # nothing during startup is a person. The control loop re-asserts
                # what the switch should be; after the window, flips count again.
                self._debug_log(
                    "switch changed to %s during startup grace; not treating as manual intent",
                    new_state.state,
                )
                self.hass.async_create_task(self._async_control_heating())
            elif (
                state_changed
                and (current := self.hass.states.get(self.heater_entity_id)) is not None
                and current.state != new_state.state
            ):
                # Stale: a later state has already landed, so this event no
                # longer describes the switch. Judging it against a command
                # baseline that has moved on is how our own turn_off echo, or
                # a reconnect replay, gets read as a person -- and, with a fast
                # enough switch, how override and control chase each other.
                self._debug_log(
                    "switch event %s is stale (switch now %s); not treating as manual intent",
                    new_state.state,
                    current.state,
                )
            elif state_changed and self._consume_own_echo(new_state.state):
                # Our own command reporting back. It is not a person even when
                # the intent has moved on while it was in flight: turn off then
                # straight back to electric, and the OFF lands after the
                # baseline is ON again. Read against the baseline, that echo
                # was a "manual OFF" -- the tank went off with a 3-hour pause,
                # against the person's last choice. Re-assert the intent.
                self._debug_log("switch reported %s: our own command", new_state.state)
                if new_state.state != self._last_commanded_switch_state:
                    self.hass.async_create_task(self._async_control_heating())
            elif returned_from_unavailable:
                # Not a person -- but not proof the relay agrees with us
                # either. A command sent while the switch was unavailable is
                # dropped (Home Assistant skips unavailable entities), and a
                # relay can come back in either state. Left alone, a relay that
                # reappeared ON stayed ON through a load shed, or with Smart Eco
                # parking the tank, until the next temperature reading -- which
                # an idle tank may not send for half an hour. Re-run control so
                # the switch is put back where the intent says, the same way
                # the own-echo branch above re-asserts it.
                self._debug_log(
                    "switch reappeared as %s after %s; not treating as manual intent, re-asserting control",
                    new_state.state,
                    "no previous state" if old_state is None else old_state.state,
                )
                self.hass.async_create_task(self._async_control_heating())
            elif (
                # A person flips the switch; an attribute-only update (rssi,
                # power) does not. Without this, a switch that publishes
                # attributes while an OFF is held back by min_on_duration --
                # baseline already OFF, relay still ON -- was read as a manual
                # ON: a Smart Eco pause and a tank that kept heating.
                state_changed
                and self._last_commanded_switch_state is not None
                and new_state.state != self._last_commanded_switch_state
            ):
                _LOGGER.debug("Manual switch override detected: %s", new_state.state)
                self.hass.async_create_task(
                    self._async_handle_manual_switch_override(new_state.state)
                )
            elif had_pending and state_changed:
                # If a delayed ON was pending and user flips back to OFF, honor OFF explicitly.
                if self._pending_switch_state == STATE_ON and new_state.state == STATE_OFF:
                    _LOGGER.debug(
                        "Manual OFF detected while delayed ON was pending; forcing OFF mode",
                    )
                    self._debug_log(
                        "manual OFF superseded delayed ON intent -> forcing OFF mode",
                    )
                    self.hass.async_create_task(
                        self._async_handle_manual_switch_override(new_state.state)
                    )
                else:
                    _LOGGER.debug(
                        "Manual switch override detected from pending action: %s",
                        new_state.state,
                    )
                    self.hass.async_create_task(
                        self._async_handle_manual_switch_override(new_state.state)
                    )

            if state_changed:
                self._pending_switch_state = None

        if self._surplus_active:
            self._note_boost_heating(dt_util.utcnow())
        self._debug_log_hvac_action("switch state update")
        self._update_smart_eco_state()
        self.async_write_ha_state()

    def _consume_own_echo(self, state: str) -> bool:
        """Match a reported state to the oldest command of ours awaiting it."""
        cutoff = dt_util.utcnow() - OWN_ECHO_WINDOW
        self._commands_in_flight = [
            (s, at) for s, at in self._commands_in_flight if at >= cutoff
        ]
        for index, (sent, _at) in enumerate(self._commands_in_flight):
            if sent == state:
                del self._commands_in_flight[index]
                return True
        return False

    async def _async_handle_manual_switch_override(self, new_switch_state: str) -> None:
        """Translate manual switch actions into operation mode intent."""
        self._debug_log("=== manual override detected: new_state=%s, current_mode=%s ===", new_switch_state, self._current_operation)

        # Keep the command baseline aligned with the observed physical switch state.
        # Without this, a manual ON path that does not remap mode can leave a stale
        # baseline (e.g. still OFF), causing subsequent manual OFF events to be ignored.
        self._last_commanded_switch_state = new_switch_state
        # The human has taken over, so any fleet hold we were advertising is no
        # longer what is deciding this heater's state.
        self._fleet_hold_reason = None
        await self._async_pause_smart_eco_for_manual_override(
            "manual_on" if new_switch_state == STATE_ON else "manual_off",
            source="manual_switch",
        )

        if new_switch_state == STATE_ON:
            self._clear_load_shed("physical switch")
            if self._current_operation == STATE_OFF:
                # Manual ON from OFF: use threshold-aware remap.
                if self._electric_mode_wants_heating():
                    self._log_debug_decision(
                        "manual switch ON from OFF -> ELECTRIC (current=%.1f <= target-cold=%.1f)",
                        self._current_temperature,
                        self._target_temperature - self._cold_tolerance,
                    )
                    await self.async_set_operation_mode(STATE_ELECTRIC)
                else:
                    lower_threshold = None
                    if self._target_temperature is not None:
                        lower_threshold = self._target_temperature - self._cold_tolerance
                    self._log_debug_decision(
                        "manual switch ON from OFF -> PERFORMANCE (current=%s > target-cold=%s, forcing heat)",
                        self._current_temperature,
                        lower_threshold,
                    )
                    await self.async_set_operation_mode(STATE_PERFORMANCE)
            elif self._current_operation == STATE_ELECTRIC:
                # Manual ON while electric would stay idle means user wants immediate heat.
                if not self._electric_mode_wants_heating():
                    self._log_debug_decision(
                        "manual switch ON from ELECTRIC -> PERFORMANCE (electric idle at current=%s, target-cold=%s)",
                        self._current_temperature,
                        None
                        if self._target_temperature is None
                        else self._target_temperature - self._cold_tolerance,
                    )
                    await self.async_set_operation_mode(STATE_PERFORMANCE)
                else:
                    self._log_debug_decision(
                        "manual switch ON while ELECTRIC already requests heat (rare mismatch path; current=%s <= target-cold=%s); no mode remap needed",
                        self._current_temperature,
                        self._target_temperature - self._cold_tolerance,
                    )
            elif self._current_operation == STATE_PERFORMANCE:
                self._log_debug_decision(
                    "manual switch ON while PERFORMANCE; no mode remap needed",
                )
            else:
                self._log_debug_decision(
                    "manual switch ON while mode=%s; no mode remap needed",
                    self._current_operation,
                )
            return

        if new_switch_state == STATE_OFF:
            self._log_debug_decision(
                "manual switch OFF in %s -> OFF (hard manual intent, bypass eco remap)",
                self._current_operation,
            )
            await self.async_set_operation_mode(STATE_OFF)

    def _electric_mode_wants_heating(self) -> bool:
        """Return whether ELECTRIC mode would currently request heat."""
        if self._current_temperature is None or self._target_temperature is None:
            return False

        # The boost target here is for consistency, not covered: it only moves
        # the Smart Eco state label and the manual-on idle check.
        return self._current_temperature <= (
            self._effective_target_temperature() - self._cold_tolerance
        )

    def _debug_log(self, message: str, *args) -> None:
        """Log debug diagnostics when debug mode is enabled."""
        if not self._debug_logging:
            return
        _LOGGER.debug("%s [debug]: " + message, self.name, *args)

    def _debug_log_hvac_action(self, source: str) -> None:
        """Log hvac action transitions when debug mode is enabled."""
        if not self._debug_logging:
            return
        action = self.hvac_action
        if action == self._last_debug_hvac_action:
            return
        self._debug_log(
            "hvac_action changed: %s -> %s (%s)",
            self._last_debug_hvac_action,
            action,
            source,
        )
        self._last_debug_hvac_action = action

    def _log_debug_decision(self, message: str, *args) -> None:
        """Log manual override decision details when debug mode is enabled."""
        self._debug_log(message, *args)

    async def _async_control_heating(self):
        """Check if we need to turn heating on or off."""
        _LOGGER.debug(
            "%s: control_heating start -> operation=%s, current_temperature=%s, target=%s, cold_tolerance=%s, hot_tolerance=%s",
            self.name,
            self._current_operation,
            self._current_temperature,
            self._target_temperature,
            self._cold_tolerance,
            self._hot_tolerance,
        )
        _LOGGER.debug("%s: debug_logging flag is: %s", self.name, self._debug_logging)
        self._debug_log(
            "=== control_heating entry: mode=%s, current_temp=%s, target=%s, smart_eco_mode=%s, pause_reason=%s, eco_condition_met=%s ===",
            self._current_operation,
            self._current_temperature,
            self._target_temperature,
            self._smart_eco_mode,
            self._smart_eco_pause_reason,
            self._eco_condition_met,
        )

        # Checked before the load-shed return below, so a tank held down by the
        # balancer for days still abandons a hopeless cycle rather than keeping
        # the request standing forever with nobody told.
        self._check_disinfection_give_up()
        # Before any early return below: the OFF and PERFORMANCE branches both
        # return, and a bound that only runs when control happens to reach the
        # bottom is not a bound.
        self._async_check_off_until_target()

        # Load shedding outranks everything, including Smart Eco's Always ON
        # policy: it protects the supply, it is not a user preference. It is
        # checked before the Smart Eco block precisely so that the "eco allows
        # heating -> restore heating mode" branch below cannot undo a shed.
        # The operation mode is left untouched, so whatever the user asked for
        # is still there when the shed is released.
        if self._load_shed:
            self._debug_log(
                "decision: load shed active -> switch OFF (mode %s and eco policy preserved)",
                self._current_operation,
            )
            await self._async_heater_turn_off(force=True)
            self._debug_log_hvac_action("load shed")
            self._update_smart_eco_state()
            self.async_write_ha_state()
            return

        smart_eco_active = self._is_smart_eco_enforcing()

        # Smart Eco parks the mode at OFF while it blocks, and only its own
        # restore branch below ever lifts that. Turn Smart Eco off at that
        # moment -- or restart into it -- and the branch never runs, leaving a
        # disinfecting tank stuck OFF until the give-up bound. Scoped to an
        # active cycle on purpose: a tank OFF for any other reason may well be
        # OFF because a person wants it that way, and guessing there would
        # energise an element nobody asked for.
        if (
            self._disinfecting
            and not smart_eco_active
            and self._current_operation == STATE_OFF
        ):
            self._debug_log("disinfection: reclaiming a tank left parked at OFF")
            self._current_operation = STATE_PERFORMANCE

        if (
            self._smart_eco_mode == SMART_ECO_MODE_OFF_UNTIL_TARGET
            and self._off_until_target_reclaim
            and self._current_operation == STATE_OFF
        ):
            # Same reclaim, same reason. Eco parks the mode at OFF while it
            # blocks and only its own restore branch lifts that -- a branch this
            # mode deliberately skips. Without this, choosing "off until target
            # reached" on a tank eco had already parked would leave it sitting
            # off, the exact opposite of what was asked for.
            #
            # Gated and ONE-SHOT, for the reason the sibling block above gives:
            # an unscoped version cannot tell the gate's OFF from a person's, so
            # it would energise a tank somebody switched off, and -- worse --
            # reverse every later turn_off for as long as the bypass lasts,
            # making OFF unreachable. It resumes _smart_eco_last_heating_mode,
            # which can be PERFORMANCE; that is deliberate parity with the eco
            # restore branch below, and safe only because this fires once, in
            # the same breath as the mode change, on a tank the gate had parked.
            resumed = self._smart_eco_last_heating_mode
            if resumed not in (STATE_ELECTRIC, STATE_PERFORMANCE):
                resumed = STATE_ELECTRIC
            self._debug_log("off-until-target: reclaiming a tank parked at OFF")
            self._current_operation = resumed
            self._off_until_target_reclaim = False
        if smart_eco_active and self._startup_grace_active and not self._eco_condition_met:
            # Neither park nor restore: the mode restored from before the
            # restart stands, and controls to target as it did then. A true
            # condition is trusted at once -- only a false one is suspect.
            self._debug_log("decision: startup grace, eco condition not yet trusted -> keeping %s", self._current_operation)
        elif smart_eco_active:
            if not self._eco_condition_met:
                if self._current_operation != STATE_OFF:
                    self._debug_log("decision: smart eco blocks heating -> setting operation mode OFF")
                    self._current_operation = STATE_OFF
                await self._async_heater_turn_off()
                self._update_smart_eco_state()
                self._debug_log_hvac_action("smart eco blocked")
                self.async_write_ha_state()
                return

            if self._current_operation == STATE_OFF:
                desired_heating_mode = self._smart_eco_last_heating_mode
                if desired_heating_mode not in (STATE_ELECTRIC, STATE_PERFORMANCE):
                    desired_heating_mode = STATE_ELECTRIC
                self._debug_log(
                    "decision: smart eco allows heating -> restoring heating mode %s",
                    desired_heating_mode,
                )
                self._current_operation = desired_heating_mode

        # If the water heater mode is explicitly OFF (and Smart Eco did not restore a heating mode),
        # ensure underlying switch is off.
        if self._current_operation == STATE_OFF:
            _LOGGER.debug("%s: operation is OFF, turning underlying switch off", self.name)
            self._debug_log("decision: mode OFF -> switch OFF")
            await self._async_heater_turn_off()
            self._debug_log_hvac_action("mode OFF")
            self._update_smart_eco_state()
            self.async_write_ha_state()
            return

        # Failsafe: the sensor reported it has no reading, so no heat until it
        # has one again. Held here, in the control pass, rather than only where
        # the sensor reports, because every retry and reconcile comes back
        # through this pass: PERFORMANCE needs no temperature to decide and
        # switched the element straight back on, and an OFF deferred by
        # min_on_duration was retried through the missing-temperature skip
        # below and never sent. Forced past min_on_duration for the same reason
        # a shed is. A disinfection cycle cannot bank its hold without a
        # reading either; its give-up bound, checked at the top, still runs.
        if self._temperature_lost:
            _LOGGER.debug("%s: temperature reading lost, holding the heater off", self.name)
            self._debug_log("decision: temperature reading lost -> switch OFF (failsafe)")
            await self._async_heater_turn_off(force=True)
            self._debug_log_hvac_action("failsafe: no temperature")
            self._update_smart_eco_state()
            self.async_write_ha_state()
            return

        # Logic for PERFORMANCE: Heat continuously while not blocked by Smart Eco.
        if self._current_operation == STATE_PERFORMANCE:
            _LOGGER.debug("%s: operation is PERFORMANCE, turning ON", self.name)
            self._debug_log("decision: mode PERFORMANCE -> switch ON")
            await self._async_heater_turn_on()
            self._debug_log_hvac_action("mode PERFORMANCE")
            self._update_smart_eco_state()
            self.async_write_ha_state()
            return

        # No reading yet (setup runs control before the sensor is read, and a
        # restart may load the sensor's integration after this one), or no
        # target: just update state.
        if (
            self._current_temperature is None
            or self._target_temperature is None
        ):
            _LOGGER.debug("%s: missing temperature/target, skipping control", self.name)
            self._debug_log("decision: skip control due to missing temperature or target")
            self._debug_log_hvac_action("missing temperature/target")
            self.async_write_ha_state()
            return

        # Control heating based on tolerance
        # Logic: Turn ON if temp <= target - cold_tolerance. Turn OFF if temp >= target + hot_tolerance.
        target = self._effective_target_temperature()
        lower_threshold = target - self._cold_tolerance
        # A boost stops AT its target (4.5.1), so the number the owner set is
        # the peak: with the hot tolerance on top, a 61 C boost ran to 64 C. The
        # cold tolerance still decides when it starts again.
        if self._surplus_boost_active():
            upper_threshold = target
        else:
            upper_threshold = target + self._hot_tolerance
        
        self._debug_log(
            "thresholds: current=%.1f, lower=%.1f (target-%.1f), upper=%.1f (target+%.1f)",
            self._current_temperature,
            lower_threshold,
            self._cold_tolerance,
            upper_threshold,
            self._hot_tolerance,
        )
        
        if self._current_temperature <= lower_threshold and self._current_operation == STATE_ELECTRIC:
            _LOGGER.debug("%s: current <= (target - cold_tolerance) -> turning ON", self.name)
            self._debug_log("decision: current %.1f <= threshold %.1f (%.1f°C gap) -> switch ON", self._current_temperature, lower_threshold, lower_threshold - self._current_temperature)
            await self._async_heater_turn_on()
        elif self._current_temperature >= upper_threshold:
            _LOGGER.debug("%s: current >= (target + hot_tolerance) -> turning OFF", self.name)
            self._debug_log("decision: current %.1f >= threshold %.1f (%.1f°C gap) -> switch OFF", self._current_temperature, upper_threshold, self._current_temperature - upper_threshold)
            await self._async_heater_turn_off()
        else:
            # Else: stay in current state (hysteresis band)
            self._debug_log("decision: within hysteresis band [%.1f, %.1f], maintaining current state", lower_threshold, upper_threshold)

        self._debug_log_hvac_action("hysteresis control")
        if self._smart_eco_pause_reason == "manual_on_wait_idle":
            self._async_check_manual_on_resume()
        self._update_smart_eco_state()
        self.async_write_ha_state()

    @callback
    def _async_schedule_cooldown_retry(self, delay: float) -> None:
        """(Re)start the cooldown timer that retries a deferred command."""
        if self._cooldown_timer:
            self._cooldown_timer()
        self._cooldown_timer = async_call_later(
            self.hass, max(float(delay), 0.1), self._async_control_heating_callback
        )

    @callback
    def _async_fleet_unregister(self) -> None:
        """Leave the fleet, leaving its stagger anchor behind for a reload."""
        self._fleet.unregister(self._entry_id, dt_util.utcnow())

    async def _async_control_heating_callback(self, _now):
        """Callback for delayed control heating."""
        self._cooldown_timer = None
        self._pending_switch_state = None
        self._debug_log("cooldown timer fired: retrying control heating")
        await self._async_control_heating()

    async def _async_heater_turn_on(self):
        """Turn heater toggleable device on."""
        now = dt_util.utcnow()
        if self._last_switch_change_time:
            delta = now - self._last_switch_change_time
            if delta < self._min_off_duration:
                _LOGGER.debug("Cooldown active (min_off_duration), delaying turn_on")
                remaining = (self._min_off_duration - delta).total_seconds()
                self._pending_switch_state = STATE_ON
                # Mark intended switch target now so opposite manual toggles are treated as overrides.
                self._last_commanded_switch_state = STATE_ON
                self._debug_log(
                    "cooldown: ON blocked by min_off_duration (elapsed=%.1fs, required=%.1fs, remaining=%.1fs); retrying in %.1fs",
                    delta.total_seconds(),
                    self._min_off_duration.total_seconds(),
                    remaining,
                    remaining,
                )
                self._debug_log("cooldown timer started for turn_on retry (%.1fs)", remaining)
                self._async_schedule_cooldown_retry(remaining)
                return

            self._pending_switch_state = None

        heater = self.hass.states.get(self.heater_entity_id)
        if heater is not None and heater.state == STATE_ON:
            # Already drawing. Tell the fleet rather than asking it for
            # permission: the load is on the inverter regardless of what we
            # decide, and a sibling must be spaced against it. This also makes
            # the request below a free re-affirmation, so a running heater can
            # never be handed a stagger hold or a retry it cannot use.
            self._fleet.note_switch_on(self._entry_id, now)

        # Fleet-wide switch-on staggering. Several instances driven by one
        # shared eco template all reach this point in the same event-loop pass,
        # so this is the only place that can stop them stepping the inverter in
        # one go.
        decision = self._fleet.request_turn_on(self._entry_id, now)
        if not decision.admitted:
            self._fleet_hold_reason = decision.reason
            self._pending_switch_state = STATE_ON
            # Mark intended switch target now so opposite manual toggles are
            # treated as overrides, exactly as the cooldown path does.
            self._last_commanded_switch_state = STATE_ON
            _LOGGER.debug(
                "%s: fleet deferred turn_on (%s); retrying in %.1fs",
                self.name,
                decision.reason,
                decision.retry_after,
            )
            self._debug_log(
                "fleet: ON deferred -- %s; retrying in %.1fs",
                decision.reason,
                decision.retry_after,
            )
            self._async_schedule_cooldown_retry(decision.retry_after)
            return

        self._fleet_hold_reason = None
        self._last_commanded_switch_state = STATE_ON
        if heater is None or heater.state == STATE_ON:
            return

        _LOGGER.debug("Turning on heater %s", self.heater_entity_id)
        self._debug_log("service call: turn_on entity_id=%s", self.heater_entity_id)
        self._last_switch_change_time = now
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        self._commands_in_flight.append((STATE_ON, now))
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_ON, data, context=self._context
        )
        self._debug_log("service call completed: turn_on entity_id=%s", self.heater_entity_id)

    async def _async_heater_turn_off(self, force: bool = False):
        """Turn heater toggleable device off.

        ``force`` skips the min_on_duration hold. Load shedding uses it: that
        minimum exists to stop thermostat noise short-cycling the relay, and
        letting it delay a supply-protection action would hand the balancer a
        shed that silently has not happened yet.
        """
        now = dt_util.utcnow()
        if not force and self._last_switch_change_time:
            delta = now - self._last_switch_change_time
            if delta < self._min_on_duration:
                _LOGGER.debug("Cooldown active (min_on_duration), delaying turn_off")
                remaining = (self._min_on_duration - delta).total_seconds()
                self._pending_switch_state = STATE_OFF
                # Mark intended switch target now so opposite manual toggles are treated as overrides.
                self._last_commanded_switch_state = STATE_OFF
                self._debug_log(
                    "cooldown: OFF blocked by min_on_duration (elapsed=%.1fs, required=%.1fs, remaining=%.1fs); retrying in %.1fs",
                    delta.total_seconds(),
                    self._min_on_duration.total_seconds(),
                    remaining,
                    remaining,
                )
                self._debug_log("cooldown timer started for turn_off retry (%.1fs)", remaining)
                self._async_schedule_cooldown_retry(remaining)
                return

            self._pending_switch_state = None
        self._last_commanded_switch_state = STATE_OFF
        # A deferred ON is no longer outstanding once OFF is the settled
        # intent, so stop reporting a stagger hold for it. The element is no
        # longer believed to be drawing either -- but its anchor stays, since it
        # records when it last STARTED, which is what spacing is measured from.
        self._fleet_hold_reason = None
        self._fleet.note_switch_off(self._entry_id)
        heater = self.hass.states.get(self.heater_entity_id)
        if heater is None or heater.state == STATE_OFF:
            return

        _LOGGER.debug("Turning off heater %s", self.heater_entity_id)
        self._debug_log("service call: turn_off entity_id=%s", self.heater_entity_id)
        self._last_switch_change_time = now
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        self._commands_in_flight.append((STATE_OFF, now))
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_OFF, data, context=self._context
        )
        self._debug_log("service call completed: turn_off entity_id=%s", self.heater_entity_id)
