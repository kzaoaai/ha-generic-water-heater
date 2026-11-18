"""Disinfection (legionella) cycle logic for GenericWaterHeater."""
from datetime import timedelta
from homeassistant.components.water_heater import STATE_PERFORMANCE, STATE_ELECTRIC
from homeassistant.const import STATE_OFF
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util
from . import (
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
    celsius_system,
    legionella_signal,
    DISINFECTION_GIVE_UP_DAYS,
    LEGIONELLA_BYPASS_ECO_MODES,
    LEGIONELLA_MODE_OFF,
    LEGIONELLA_MODES,
    LEGIONELLA_ONE_SHOT_MODES,
)
from .water_heater_const import (
    ISSUE_DISINFECTION_GAVE_UP,
    _LOGGER,
)


class DisinfectionMixin:
    """Disinfection (legionella) cycle logic for GenericWaterHeater."""

    async def async_set_legionella_mode(self, mode: str, source: str) -> None:
        """Set the disinfection policy for this tank."""
        if mode not in LEGIONELLA_MODES:
            return
        if mode != LEGIONELLA_MODE_OFF and not celsius_system(self.hass):
            # Nothing could complete the cycle: the sensor that judges a hold
            # is inert here (see celsius_system), so a one-shot would hold the
            # element on in performance until the give-up bound, days later.
            _LOGGER.warning(
                "%s: disinfection works in Celsius only; the policy stays off",
                self.name,
            )
            # The select wrote its choice to runtime before asking.
            self._runtime["legionella_mode"] = self._legionella_mode
            self._notify_legionella_select()
            return
        if self._legionella_mode == mode:
            return

        self._legionella_mode = mode
        self._runtime["legionella_mode"] = mode
        # A person choosing a policy is fresh intent; a give-up backoff left by
        # the previous one does not carry over.
        self._disinfection_retry_after = None
        self._clear_issue(ISSUE_DISINFECTION_GAVE_UP)
        self._debug_log("disinfection policy: %s (source=%s)", mode, source)

        if mode == LEGIONELLA_MODE_OFF and self._disinfecting:
            self._end_disinfection("policy turned off")
            await self._async_control_heating()
        else:
            # Choosing a policy IS a person asking for heat, so it outranks a
            # mode nobody has touched since. The interval lapsing later is not.
            await self._async_evaluate_disinfection(user_request=True)

        self._notify_legionella_select()
        self.async_write_ha_state()

    async def _async_evaluate_disinfection(self, user_request: bool = False) -> None:
        """Start or finish a cycle from the risk sensor's own verdict.

        ``user_request`` marks the call that follows a person choosing a policy,
        as opposed to the risk sensor reporting on its own later.
        """
        risk = self._runtime.get("legionella_risk")

        if self._disinfecting:
            if self._cycle_completed_since_start():
                self._end_disinfection("cycle completed", completed=True)
                await self._async_control_heating()
                return
            # Upgrading a cycle that is already waiting on the eco window is
            # the obvious thing to reach for, and this early return is what
            # would otherwise swallow it: the bypass lives on the start path,
            # and a cycle in flight never goes back through it.
            if (
                user_request
                and self._legionella_mode in LEGIONELLA_BYPASS_ECO_MODES
                and self._smart_eco_mode != SMART_ECO_MODE_OFF_UNTIL_TARGET
            ):
                self._off_until_target_for_disinfection = True
                await self.async_set_smart_eco_mode(
                    SMART_ECO_MODE_OFF_UNTIL_TARGET, source="disinfect_asap"
                )
                await self._async_control_heating()
            return

        if self._legionella_mode == LEGIONELLA_MODE_OFF:
            return
        # A one-shot is a command: it runs now even on a tank whose risk is
        # still Low. Waiting for the interval instead left it armed and silent
        # for days, then fired at a moment nobody picked. Not limited to the
        # moment of choosing -- an armed one-shot seen later (after a restart,
        # say) is the same pending command, and it clears itself to Off once
        # its cycle completes, so it cannot run twice. A standing policy
        # (As needed) still waits for the risk to rise.
        if (
            risk not in ("Elevated", "High")
            and self._legionella_mode not in LEGIONELLA_ONE_SHOT_MODES
        ):
            return
        if self._legionella_mode not in LEGIONELLA_ONE_SHOT_MODES and self._in_give_up_backoff():
            # A standing policy that just gave up would otherwise restart at
            # once -- the risk is still Elevated -- and spend another three
            # days failing in the same way.
            return

        # A tank a person has switched off stays off -- unless they have just
        # asked for a cycle NOW. "Until disinfected" is a command; "On" is a
        # standing policy, and a policy does not overrule a mode someone chose.
        # This matters because once Smart Eco is itself switched off there is no
        # way to tell an eco-parked OFF from a deliberate one -- and that is
        # exactly the configuration for forcing a cycle through on a tank that
        # cannot reach temperature inside a PV window, so swallowing the request
        # made the documented workaround do nothing at all.
        asked_for_now = (
            user_request and self._legionella_mode in LEGIONELLA_ONE_SHOT_MODES
        )
        eco_parked = self._is_smart_eco_enforcing() and not self._eco_condition_met
        if self._current_operation == STATE_OFF and not eco_parked and not asked_for_now:
            self._debug_log("disinfection: tank is off by request, not starting")
            return

        # What "normal operation" means for this tank when the cycle ends. An
        # OFF written by the eco gate is not the owner's intent, so come back to
        # what it parked; an OFF the owner chose is, so come back to OFF.
        prior = (
            self._smart_eco_last_heating_mode
            if eco_parked
            else self._current_operation
        )
        # Never stash PERFORMANCE as the mode to come back to. Reverting a
        # disinfection cycle into an unbounded performance run is the failure
        # this whole feature exists to avoid.
        if prior not in (STATE_ELECTRIC, STATE_OFF):
            prior = STATE_ELECTRIC

        self._disinfecting = True
        self._clear_issue(ISSUE_DISINFECTION_GAVE_UP)
        self._disinfection_prior_mode = prior
        self._disinfection_started_at = dt_util.utcnow().isoformat()
        self._current_operation = STATE_PERFORMANCE
        # Carry the request across the nightly eco gap: the eco gate parks the
        # mode at OFF and restores this value when the condition returns.
        self._smart_eco_last_heating_mode = STATE_PERFORMANCE
        self._runtime["smart_eco_last_heating_mode"] = STATE_PERFORMANCE
        if self._legionella_mode in LEGIONELLA_BYPASS_ECO_MODES:
            # "Once" / "As needed" (no "(eco)") mean do not wait for the eco
            # condition. Expressed as the transient eco mode rather than as a
            # second bypass of its own, so
            # there is one mechanism that stands eco down and one that gives it
            # back -- and the give-back is already bounded.
            self._off_until_target_for_disinfection = True
            self.hass.async_create_task(
                self.async_set_smart_eco_mode(
                    SMART_ECO_MODE_OFF_UNTIL_TARGET, source="disinfect_asap"
                )
            )

        _LOGGER.info("%s: disinfection cycle started (risk=%s)", self.name, risk)
        self._debug_log("disinfection: started, will return to %s", prior)
        await self._async_control_heating()
        self.async_write_ha_state()

    def _in_give_up_backoff(self) -> bool:
        """Whether a standing policy is still waiting out a give-up."""
        if self._disinfection_retry_after is None:
            return False
        retry = dt_util.parse_datetime(self._disinfection_retry_after)
        if retry is None or dt_util.utcnow() >= retry:
            self._disinfection_retry_after = None
            return False
        return True

    def _cycle_completed_since_start(self) -> bool:
        """Whether a full hold has completed since this cycle began.

        Not "risk is Low": a one-shot can start on a Low tank, and reading the
        verdict would end that cycle the moment it began. A hold completing
        after the start is what the cycle was for, whatever the risk said.
        """
        last = self._runtime.get("legionella_last_disinfection_at")
        started = (
            dt_util.parse_datetime(self._disinfection_started_at)
            if self._disinfection_started_at
            else None
        )
        return last is not None and started is not None and last >= started

    @callback
    def _end_disinfection(
        self, reason: str, completed: bool = False, stand_down: bool = False
    ) -> None:
        """Finish a cycle and hand the tank back. Caller re-runs control.

        ``stand_down`` also sets the policy to Off. A person who takes the tank
        back mid-cycle should not have it pulled into PERFORMANCE again by a
        standing policy the moment the risk sensor next reports; re-arming is
        one tap, and the select shows plainly that it is no longer armed.
        """
        if not self._disinfecting:
            return

        prior = self._disinfection_prior_mode or STATE_ELECTRIC
        self._disinfecting = False
        self._disinfection_prior_mode = None
        self._disinfection_started_at = None

        if self._current_operation == STATE_PERFORMANCE:
            self._current_operation = prior
        # Also correct what the eco gate would restore, whether or not the mode
        # is parked at OFF right now. That field only ever holds a heating mode,
        # so a cycle returning the tank to OFF still has to clear the PERFORMANCE
        # it wrote on the way in -- otherwise re-enabling Smart Eco later would
        # restore an unbounded performance run.
        resume_mode = prior if prior == STATE_PERFORMANCE else STATE_ELECTRIC
        self._smart_eco_last_heating_mode = resume_mode
        self._runtime["smart_eco_last_heating_mode"] = resume_mode

        one_shot_done = completed and self._legionella_mode in LEGIONELLA_ONE_SHOT_MODES
        if one_shot_done or stand_down:
            self._legionella_mode = LEGIONELLA_MODE_OFF
            self._runtime["legionella_mode"] = LEGIONELLA_MODE_OFF
            self._notify_legionella_select()

        if (
            self._off_until_target_for_disinfection
            and self._smart_eco_mode == SMART_ECO_MODE_OFF_UNTIL_TARGET
        ):
            # The bypass was taken out FOR this cycle, so the cycle ending is
            # what should end it -- not the 6 h bound. Waiting for the bound is
            # not a harmless delay: a cancelled or abandoned cycle usually
            # leaves the tank OFF, hvac_action then reads "off" rather than
            # "idle", and the satisfied path can never fire at all, so the full
            # window always elapses with eco standing down for no live reason.
            self._off_until_target_for_disinfection = False
            self.hass.async_create_task(
                self._async_restore_previous_eco_mode(f"cycle ended: {reason}")
            )

        _LOGGER.info("%s: disinfection cycle ended (%s)", self.name, reason)
        self._debug_log("disinfection: ended (%s), back to %s", reason, prior)

    def adopt_restored_legionella_mode(self, mode: str) -> None:
        """Take the policy the select restored, when we restored none.

        Not ``async_set_legionella_mode``: that is a person choosing, which
        starts a one-shot now and drops a give-up backoff. A restore is neither.
        """
        if mode not in LEGIONELLA_MODES:
            return
        refused = (
            mode != LEGIONELLA_MODE_OFF
            and self.hass is not None
            and not celsius_system(self.hass)
        )
        if refused:
            mode = LEGIONELLA_MODE_OFF
        self._legionella_mode = mode
        self._runtime["legionella_mode"] = mode
        if self.hass is not None:
            self.async_write_ha_state()
        if refused:
            # The select may have set up first and be showing what it
            # restored; nothing else would tell it the policy is not running.
            self._notify_legionella_select()

    @callback
    def _notify_legionella_select(self) -> None:
        """Refresh the policy select after the integration changes it."""
        async_dispatcher_send(
            self.hass, legionella_signal(self._device_identifier), self._legionella_mode
        )
        select_entity = self._runtime.get("legionella_select_entity")
        if select_entity is not None:
            select_entity.async_write_ha_state()

    @callback
    def _check_disinfection_give_up(self) -> bool:
        """Abandon a cycle that is getting nowhere. Returns True if abandoned.

        Deliberately a calendar bound on the whole request, not a cap on run
        length: most of a hold is banked unpowered while the tank coasts down
        over an evening, so a run-length cap would abort the part that earns the
        credit. A tank that cannot get there in three days is not going to --
        one of the two here provably cannot reach 60 C inside a PV window at
        all -- and the owner should hear about it rather than keep paying for it.
        """
        if not self._disinfecting or self._disinfection_started_at is None:
            return False

        started = dt_util.parse_datetime(self._disinfection_started_at)
        if started is None:
            # Unreadable stamp: re-stamp rather than leave the cycle unbounded
            # forever. Costs at most one extra interval, never an escape.
            _LOGGER.warning(
                "%s: unreadable disinfection start time %r, restarting the bound",
                self.name,
                self._disinfection_started_at,
            )
            self._disinfection_started_at = dt_util.utcnow().isoformat()
            return False
        if dt_util.utcnow() - started < timedelta(days=DISINFECTION_GIVE_UP_DAYS):
            return False

        progress = self._runtime.get("legionella_hold_progress_minutes")
        if self._load_shed:
            blocked_by = (
                "The load balancer is currently shedding this tank, which may be "
                "what stopped it getting there."
            )
        elif self._is_smart_eco_enforcing() and not self._eco_condition_met:
            blocked_by = (
                "Smart Eco is gating it -- choose Once to push a cycle through "
                "without waiting for the eco condition."
            )
        else:
            blocked_by = (
                "Nothing is blocking it right now, so the element most likely "
                "cannot reach 60 °C at this tank's sensor."
            )
        self._end_disinfection("no disinfection reached in time")
        if self._legionella_mode in LEGIONELLA_ONE_SHOT_MODES:
            # A one-shot was a single request; it is over either way.
            self._legionella_mode = LEGIONELLA_MODE_OFF
            self._runtime["legionella_mode"] = LEGIONELLA_MODE_OFF
            self._notify_legionella_select()
            outcome = "it has been stopped and the policy set to Off"
        else:
            # A standing policy stays armed -- setting it Off would silently
            # disarm it -- but waits one full interval before trying again.
            interval = int(self._runtime.get("legionella_interval_days") or 7)
            retry = dt_util.utcnow() + timedelta(days=interval)
            self._disinfection_retry_after = retry.isoformat()
            outcome = (
                "it has been stopped; the policy stays on and will try again "
                f"after {dt_util.as_local(retry).strftime('%Y-%m-%d %H:%M')}"
            )
        # A repair rather than a notification: it stays listed until the next
        # cycle starts or the policy changes, instead of being dismissed once.
        self._raise_issue(
            ISSUE_DISINFECTION_GAVE_UP,
            days=str(DISINFECTION_GIVE_UP_DAYS),
            outcome=outcome,
            progress=str(progress),
            blocked_by=blocked_by,
        )
        return True

    @callback
    def _async_handle_legionella_risk(self, _risk) -> None:
        """Re-evaluate the policy when the risk sensor reports a new verdict."""
        self.hass.async_create_task(self._async_evaluate_disinfection())
