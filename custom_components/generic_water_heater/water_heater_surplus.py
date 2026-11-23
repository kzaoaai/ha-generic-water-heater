"""Surplus boost and forecast pre-heat logic for GenericWaterHeater."""
from datetime import datetime
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util
from . import eco_switches_signal
from .element_health import DRAW_W, to_watts
from .fleet import NO_READING, SurplusReading
from .temperature_filter import finite
from .switch import (
    DEFAULT_BOOST_ENABLED,
    DEFAULT_USE_UTILITY,
    RUNTIME_BOOST_ENABLED,
    RUNTIME_USE_UTILITY,
)
from .water_heater_const import (
    ELEMENT_DRAW_SETTLE,
    SURPLUS_BOOST_SHORT_RUN,
    SURPLUS_BOOST_LOCKOUT,
    SURPLUS_FIT_LOCKOUT,
    SURPLUS_FIT_LOCKOUT_MAX,
    SURPLUS_DISTANCE_UNKNOWN,
    DISINFECTION_GOAL_C,
)


class SurplusBoostMixin:
    """Surplus boost and forecast pre-heat logic for GenericWaterHeater."""

    def _use_utility(self) -> bool:
        """The "Eco: Use Utility" switch."""
        return bool(self._runtime.get(RUNTIME_USE_UTILITY, DEFAULT_USE_UTILITY))

    def _surplus_boost_enabled(self) -> bool:
        """The "Boost: Use PV Excess" switch."""
        return bool(self._runtime.get(RUNTIME_BOOST_ENABLED, DEFAULT_BOOST_ENABLED))

    def _surplus_boost_configured(self) -> bool:
        return self._surplus_entity is not None and (
            self._surplus_boost_target is not None or self._forecast_preheat is not None
        )

    def _forecast_kwh(self) -> float | None:
        """Tomorrow's forecast in kWh, or None while it cannot be read."""
        state = self.hass.states.get(self._forecast_preheat["entity"])
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = (state.attributes.get("unit_of_measurement") or "kWh").strip()
        return value / 1000 if unit == "Wh" else value * 1000 if unit == "MWh" else value

    def _forecast_poor(self) -> bool:
        """Whether tomorrow's solar forecast is below the pre-heat threshold.

        An unreadable forecast is not a poor one: pre-heat stores extra heat
        on a guess, so without the forecast it does nothing. The one exception
        is a carry through a start (_carry_in_force): a forecast that was poor
        when Home Assistant went down and has not been readable since still
        counts, while the element it was heating is still on.
        """
        if self._forecast_preheat is None:
            return False
        kwh = self._forecast_kwh()
        if kwh is None:
            return self._carried_forecast_poor and self._carry_in_force()
        return kwh < self._forecast_preheat["min_kwh"]

    def _boost_target_now(self) -> float | None:
        """The boost target in force: raised to the pre-heat target before a poor day."""
        targets = [self._surplus_boost_target] if self._surplus_boost_target is not None else []
        if self._forecast_poor():
            targets.append(self._forecast_preheat["target"])
        return max(targets) if targets else None

    async def _async_forecast_changed(self, _event) -> None:
        if self._forecast_kwh() is not None:
            # Read again: from here it speaks for itself, also if it drops out.
            self._carried_forecast_poor = False
        await self._async_control_heating()
        self.async_write_ha_state()

    def _surplus_boost_locked(self) -> bool:
        return (
            self._surplus_boost_locked_until is not None
            and dt_util.utcnow() < self._surplus_boost_locked_until
        )

    def _surplus_for_boost(self) -> bool:
        """Surplus as the boost sees it: read now, or carried through a start.

        A restart must not operate the relay without a good reason. At a start
        the surplus entity is missing, unavailable or still inside its own
        delay, all of which read inactive, so a tank being boosted above its
        normal target was "satisfied" when the heater loaded: the element went
        off, and on again when the entity came back a minute later. The
        startup grace already says a false eco condition cannot be judged yet;
        this is the same allowance for the boost. Surplus that was active when
        Home Assistant went down still counts until the entity has been seen
        active again (from then on its off is a real one) or the grace ends.
        Only the reading is carried: the Boost switch, a lockout, a shed and
        the mode decide as they always do.

        And it counts only while the element reads on (_carry_in_force).
        """
        return self._surplus_active or (
            self._carried_surplus and self._carry_in_force()
        )

    def _carry_in_force(self) -> bool:
        """Whether what a start carried may be used: in the grace, element on.

        One rule for both carried readings, the surplus and the forecast. A
        carry exists to keep a running element running; it must never switch
        one on. What was saved can be stale (a crash, a power cut, hours of
        downtime), and a relay found off and switched on by a reading nobody
        has taken since is a restart operating a breaker in the worse
        direction. The surplus was gated this way first and the forecast was
        not: with the surplus read active and the forecast still unreadable,
        a carried "poor" raised the target and switched an off element on.
        The test is the relay as it reads now, not whether it was on when
        Home Assistant went down, so an element switched on by something else
        inside the grace is kept toward the boost target until the grace ends.
        """
        return self._startup_grace_active and self._element_reads_on()

    def _element_reads_on(self) -> bool:
        heater = self.hass.states.get(self.heater_entity_id)
        return heater is not None and heater.state == STATE_ON

    def _surplus_boost_active(self) -> bool:
        """Whether control is aiming at the boost target right now.

        Only in plain electric: OFF is a person's (or the gate's) and must stay
        reachable, PERFORMANCE already heats without a target, a shed outranks
        everything, and a disinfection cycle runs its own course.
        """
        return (
            self._surplus_boost_configured()
            and self._surplus_boost_enabled()
            and self._surplus_for_boost()
            and self._target_temperature is not None
            and (boost := self._boost_target_now()) is not None
            and boost > self._target_temperature
            and self._current_operation == STATE_ELECTRIC
            and not self._load_shed
            # Belt and braces, not covered: a cycle runs in performance, and a
            # person choosing electric ends it, so the electric check above
            # already excludes it.
            and not self._disinfecting
            and not self._surplus_boost_locked()
        )

    def heating_goal(self) -> float | None:
        """What the tank is heading for, for the Ready At estimate.

        60 C in a disinfection cycle (sensor.DISINFECTION_TEMP_C), otherwise
        the target control aims at.
        """
        if self._disinfecting:
            return DISINFECTION_GOAL_C
        return self._effective_target_temperature()

    def _effective_target_temperature(self):
        """The target control aims at: the boost target while boosting.

        The published target_temperature stays the person's own; the boost is
        reported separately (surplus_boost_active / surplus_boost_target).
        """
        if self._surplus_boost_active():
            return self._boost_target_now()
        return self._target_temperature

    def _surplus_heating_now(self) -> bool:
        """Whether the element is on with heat that ends when the surplus does.

        That draw is what clears a battery-judged surplus, so it is what a
        short-run lockout is judged on. Two kinds of heat qualify:

        - the boost, coarsely: active with the element on, without asking
          whether the tank was above its normal target. A boost that reached
          its target and stopped before the surplus left reads False: it did
          not cause the surplus to leave.
        - normal heating that Smart Eco allows on the surplus alone (4.6.8).
          Not while the utility (with its switch) or the template also allows
          it: that heat goes on when the surplus leaves, so the surplus
          leaving proves nothing about it. A disinfection cycle waiting for
          eco counts like any other heat under the gate.

        Never while a lockout runs. The gate parks the tank then, but an OFF
        held back by min_on_duration leaves the element on for a while, and
        the surplus flapping meanwhile would start the lockout again from
        each fall: a lockout renewed by the flapping it exists to sit out.

        The startup grace is not asked about here. What keeps a restart from
        setting a lockout is that nothing in the grace is seen to begin: see
        _handle_surplus_edge and _note_boost_heating.
        """
        if not self._element_reads_on() or self._surplus_boost_locked():
            return False
        if self._surplus_boost_active():
            return True
        return (
            self._is_smart_eco_enforcing()
            # Belt and braces, not covered: both callers ask only while the
            # surplus reads active (a fall is judged before the new reading,
            # and the relay's stamp is taken only under an active surplus).
            and self._surplus_active
            and not (self._utility_active and self._use_utility())
            and not self._eco_template_met
        )

    def _handle_surplus_edge(
        self, surplus_was_heating: bool, *, seen: bool, left: bool
    ) -> None:
        """Open a surplus window, or close one and lock out a short run.

        `seen`: this rise was watched happening. The first reading after a
        load is not (the surplus may have been there for hours), and neither
        is one inside the startup grace, where the entity comes up through
        states that mean nothing (2026-11-15: off for 60 s after a restart).
        A surplus whose start is not known is not a short one. That, with the
        relay's side of it in _note_boost_heating, is the whole of what keeps
        a restart from setting a lockout, which since 4.6.8 would park, when
        the grace ends, a tank that was heating before it.

        `left`: the surplus entity can be read and says inactive. An entity
        that dropped out reads inactive too, and stops the heat as it always
        has, but a sensor blip is not the surplus failing to carry the
        element.
        """
        now = dt_util.utcnow()
        if self._surplus_active:
            # The element's own start is stamped where it switches on
            # (_note_boost_heating). An element already on when the surplus
            # arrives is measured from the window alone.
            self._surplus_window_start = now if seen else None
            return

        # Measured from the later of surplus arriving and this run of the
        # element starting. From surplus alone, the first short boost after a
        # long sunny stretch was never locked out -- the window had been open
        # for hours -- and cost one battery-drawing run before the next was
        # (seen 2026-10-18). A run that started before the surplus did is
        # bounded by the surplus window as before.
        # With the window's start unknown (see `seen`), a run that was seen
        # starting is still measured, from its own start.
        started = self._surplus_window_start
        if self._boost_heating_since is not None:
            started = (
                self._boost_heating_since
                if started is None
                else max(started, self._boost_heating_since)
            )
        self._surplus_window_start = None
        self._boost_heating_since = None
        # A new surplus is a new question: what failed to fit the last one
        # says nothing about the next.
        self._surplus_fit_failures = 0
        if (
            surplus_was_heating
            and left
            and started is not None
            and now - started < SURPLUS_BOOST_SHORT_RUN
        ):
            self._surplus_boost_locked_until = now + SURPLUS_BOOST_LOCKOUT
            self._surplus_lock_reason = "short"
            if self._surplus_boost_lockout_timer is not None:
                self._surplus_boost_lockout_timer()
            self._surplus_boost_lockout_timer = async_call_later(
                self.hass,
                SURPLUS_BOOST_LOCKOUT.total_seconds(),
                self._async_surplus_boost_lockout_ended,
            )
            self._debug_log(
                "surplus lasted %ss with the element on; locked out until %s",
                int((now - started).total_seconds()),
                self._surplus_boost_locked_until.isoformat(),
            )

    @callback
    def _note_boost_heating(self, now: datetime, *, came_on: bool) -> None:
        """Stamp when the element came on under the surplus; clear when it stops.

        Stamped only where the run is seen to begin (`came_on`): the relay
        going from off to on, outside the startup grace. The heater switch
        also reports attribute updates, returns from dropouts and, after a
        restart, replays states with no command behind them (2026-10-02), and
        a stamp at one of those dated a run of hours as seconds old, which
        made its end a "short run".
        """
        if self._surplus_heating_now():
            if self._boost_heating_since is None and came_on:
                self._boost_heating_since = now
        else:
            self._boost_heating_since = None

    async def _async_surplus_boost_lockout_ended(self, _now) -> None:
        """Let the surplus count again."""
        self._surplus_boost_lockout_timer = None
        self._surplus_boost_locked_until = None
        self._surplus_lock_reason = None
        self._debug_log("surplus lockout ended")
        # The gate was closed on it (_recompute_eco_condition).
        self._recompute_eco_condition()
        self._update_smart_eco_state()
        # The control pass publishes the state on every one of its paths.
        await self._async_control_heating()

    # --- surplus fit (4.7.0; fleet.py, "Surplus fit") ------------------------

    def _read_surplus_power(self) -> SurplusReading:
        """The surplus power sensor as it reads now; no reading if it cannot be read."""
        if self._surplus_power_entity is None:
            return NO_READING
        state = self.hass.states.get(self._surplus_power_entity)
        value = finite(state.state) if state is not None else None
        if value is None:
            return NO_READING
        watts = to_watts(value, state.attributes.get("unit_of_measurement"))
        if watts is None:
            return NO_READING
        return SurplusReading(watts, state.attributes.get("curtailed") is True)

    def _normal_control_heats(self) -> bool:
        """Whether the plain thermostat, with no boost, would have the element on.

        Below the normal target less the cold tolerance it heats; at the target
        plus the hot tolerance it does not; in between it keeps whatever the
        element is doing, as the control pass does.
        """
        if self._current_temperature is None or self._target_temperature is None:
            return False
        if self._current_temperature <= self._target_temperature - self._cold_tolerance:
            return True
        if self._current_temperature >= self._target_temperature + self._hot_tolerance:
            return False
        return self._element_reads_on()

    def _heat_depends_on_surplus(self) -> bool:
        """Whether heat, now or about to start, would stop if the surplus left.

        This is what the fit sizes, and nothing else: heat a person asked for,
        heat the utility or the template also allows, and a tank the plain
        thermostat heats with no eco policy in force all run whatever the
        surplus power says. It is _surplus_heating_now without the relay, and
        finer on one point: where something other than the surplus lets the
        thermostat heat (no policy enforcing, or the utility or the template
        opening the gate too), only the boost's heat above what the thermostat
        would do anyway counts, where the lockout's trigger takes any boost as
        one (a tank below its normal target must never be made to wait for a
        surplus it does not need).

        Never during the startup grace: nothing there is judged yet, and a
        restart must not operate the relay for a reading it has just met.
        """
        if (
            self._startup_grace_active
            or not self._surplus_active
            or self._surplus_boost_locked()
            or self._load_shed
        ):
            return False
        if (
            self._is_smart_eco_enforcing()
            and not (self._utility_active and self._use_utility())
            and not self._eco_template_met
        ):
            # The gate is open on the surplus alone.
            return True
        # No policy in force, or the utility or the template also opens the
        # gate: the thermostat's own heat goes on without the surplus, and only
        # the boost's heat above it is the surplus's.
        return self._surplus_boost_active() and not self._normal_control_heats()

    def _surplus_fit_applies(self) -> bool:
        return self._surplus_power_entity is not None and self._heat_depends_on_surplus()

    def _surplus_distance(self) -> tuple[float, bool]:
        """How far the tank is from the goal the fit ranks it by, and whether
        that goal is only the boost's.

        The closest goes first (fleet.request_surplus). A tank below its normal
        target is ranked by its distance to that target, whatever it would go
        on to under a boost: the point is one tank with hot water before two
        that are warm. A tank that already has its normal target is ranked by
        what is left to its boost target, and after every tank that has not. A
        disinfection cycle is ranked by its own goal and never as extra. A tank
        with no reading does not heat; it is ranked last for form's sake.
        """
        current = self._current_temperature
        if current is None:
            return (SURPLUS_DISTANCE_UNKNOWN, False)
        if self._disinfecting:
            return (max(float(DISINFECTION_GOAL_C - current), 0.0), False)
        target = self._target_temperature
        if target is not None and current < target:
            return (float(target - current), False)
        goal = self.heating_goal()
        if goal is None:
            return (SURPLUS_DISTANCE_UNKNOWN, True)
        return (max(float(goal - current), 0.0), True)

    def _surplus_fit_held(self) -> bool:
        """Whether a start is waiting on the fit (the hold reason says so)."""
        return (self._fleet_hold_reason or "").startswith("surplus")

    @callback
    def _sync_surplus_claim(self) -> None:
        """Keep the fleet's record of who heats on the surplus true.

        An element that is on with heat that depends on the surplus is counted,
        however it got there (a restart, a reload, the utility leaving while it
        ran). One that is on and no longer depends on the surplus is not: it
        would be picked for a release that cannot stop it. A heater admitted
        and still waiting out the stagger keeps its place.
        """
        if self._surplus_power_entity is None:
            return
        now = dt_util.utcnow()
        if self._heat_depends_on_surplus():
            if self._element_reads_on():
                self._fleet.adopt_surplus(self._entry_id, now)
        elif self._fleet.holds_surplus(self._entry_id) and not (
            # Told to go by the fit and still on because its minimum on-time
            # holds the relay: it stays counted until the relay opens, or the
            # reading it is still loading would send its sibling off after it.
            # Only while off is still what this heater means to be: a person
            # switching it back on makes it heat that is not the surplus's.
            self._fleet.surplus_releasing(self._entry_id)
            and self._element_reads_on()
            and self._last_commanded_switch_state == STATE_OFF
        ):
            self._fleet.release_surplus(self._entry_id, now)

    @callback
    def _learn_element_draw(self) -> None:
        """Remember what the meter shows while the element is on.

        The fit needs the draw while the element is off, when the meter reads
        nothing. Kept to 50 W and moved only by a change of more than a tenth,
        so a supply sagging and recovering does not rewrite it all day.

        Nothing is written here: a meter reports every few seconds and a meter
        report writes no state (test_a_meter_report_commands_nothing_and_writes_
        nothing). The attribute goes out with the heater's next write, which a
        running element's rising temperature brings within minutes.
        """
        if not self._element_reads_on():
            return
        watts = self._meter_watts()
        if watts is None or watts < DRAW_W:
            return
        if (
            self._last_switch_change_time is not None
            and dt_util.utcnow() - self._last_switch_change_time < ELEMENT_DRAW_SETTLE
        ):
            return
        known = self._element_draw_w
        if known is not None and abs(watts - known) <= 0.1 * known:
            return
        self._element_draw_w = float(round(watts / 50) * 50)
        self._debug_log("element draw learned: %.0f W", self._element_draw_w)

    async def _async_surplus_power_changed(self, _event) -> None:
        """The surplus power sensor reported."""
        await self._async_evaluate_surplus_fit()

    async def _async_surplus_fit_due(self, _now) -> None:
        self._surplus_fit_timer = None
        await self._async_evaluate_surplus_fit()

    async def _async_evaluate_surplus_fit(self) -> None:
        """Give this heater's surplus heat up if it is its turn to.

        Starting is not decided here: a waiting heater asks again on its own
        retry timer (_async_heater_turn_on). This only answers a reading that
        has gone short, and re-arms itself for one that stays short without
        changing, since such a reading sends no further event.
        """
        if self._surplus_power_entity is None or self.hass is None:
            return
        if self._surplus_fit_timer is not None:
            self._surplus_fit_timer()
            self._surplus_fit_timer = None
        self._sync_surplus_claim()
        now = dt_util.utcnow()
        release, look_again = self._fleet.surplus_release_due(
            self._entry_id, now, self._read_surplus_power()
        )
        if release:
            await self._async_surplus_fit_release(now)
        elif look_again is not None:
            self._surplus_fit_timer = async_call_later(
                self.hass, max(look_again, 0.1), self._async_surplus_fit_due
            )

    async def _async_surplus_fit_release(self, now: datetime) -> None:
        """Stop heating on a surplus that has gone short, and wait before asking again.

        The lockout is the existing one: while it runs the surplus counts for
        neither the eco gate nor the boost, so the control pass turns the
        element off through the ordinary path (its own echo, not a person's
        OFF), and when it ends the heater asks the fit again. Its term follows
        the run: a heater released within SURPLUS_BOOST_SHORT_RUN of starting
        was a start that failed (a probe of a held-back array, or a reading
        that did not hold), and waits the full SURPLUS_BOOST_LOCKOUT like any
        short run; one that had been heating longer was overtaken by the
        afternoon or the house, and waits the shorter SURPLUS_FIT_LOCKOUT.

        With a minimum on-time the relay may stay closed for a while after
        this. The eco gate is shut meanwhile, as for any heat the gate stops
        while that minimum runs: eco_gated reads false and the label already
        says waiting.
        """
        since = self._fleet.surplus_drawing_since(self._entry_id)
        if since is None or now - since < SURPLUS_BOOST_SHORT_RUN:
            # And twice as long each time it fails again on this surplus: a
            # held-back array goes on saying "more is there" without saying
            # how much (SURPLUS_FIT_LOCKOUT_MAX).
            self._surplus_fit_failures += 1
            lockout = min(
                SURPLUS_BOOST_LOCKOUT * 2 ** (self._surplus_fit_failures - 1),
                SURPLUS_FIT_LOCKOUT_MAX,
            )
        else:
            self._surplus_fit_failures = 0
            lockout = SURPLUS_FIT_LOCKOUT
        # Counted until the relay is really off: _async_heater_turn_off drops
        # it, at once or when a minimum on-time lets the OFF through.
        self._fleet.mark_surplus_releasing(self._entry_id)
        self._surplus_boost_locked_until = now + lockout
        self._surplus_lock_reason = "fit"
        self._boost_heating_since = None
        if self._surplus_boost_lockout_timer is not None:
            self._surplus_boost_lockout_timer()
        self._surplus_boost_lockout_timer = async_call_later(
            self.hass,
            lockout.total_seconds(),
            self._async_surplus_boost_lockout_ended,
        )
        self._debug_log(
            "surplus short with this heater admitted last; released until %s",
            self._surplus_boost_locked_until.isoformat(),
        )
        self._recompute_eco_condition()
        self._update_smart_eco_state()
        await self._async_control_heating()
        self.async_write_ha_state()

    async def async_set_eco_use_utility(self, value: bool) -> None:
        """Set the "Eco: Use Utility" switch. A policy choice, not a manual override."""
        self._runtime[RUNTIME_USE_UTILITY] = bool(value)
        await self._async_eco_switch_changed()

    async def async_set_surplus_boost_enabled(self, value: bool) -> None:
        """Set the "Boost: Use PV Excess" switch. A policy choice, not a manual override."""
        self._runtime[RUNTIME_BOOST_ENABLED] = bool(value)
        await self._async_eco_switch_changed()

    async def _async_eco_switch_changed(self) -> None:
        self._recompute_eco_condition()
        async_dispatcher_send(self.hass, eco_switches_signal(self._entry_id))
        self._update_smart_eco_state()
        await self._async_control_heating()
        self.async_write_ha_state()
