"""Surplus boost and forecast pre-heat logic for GenericWaterHeater."""
from datetime import datetime
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util
from . import eco_switches_signal
from .switch import (
    DEFAULT_BOOST_ENABLED,
    DEFAULT_USE_UTILITY,
    RUNTIME_BOOST_ENABLED,
    RUNTIME_USE_UTILITY,
)
from .water_heater_const import (
    SURPLUS_BOOST_SHORT_RUN,
    SURPLUS_BOOST_LOCKOUT,
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
        if (
            surplus_was_heating
            and left
            and started is not None
            and now - started < SURPLUS_BOOST_SHORT_RUN
        ):
            self._surplus_boost_locked_until = now + SURPLUS_BOOST_LOCKOUT
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
        self._debug_log("surplus lockout ended")
        # The gate was closed on it (_recompute_eco_condition).
        self._recompute_eco_condition()
        self._update_smart_eco_state()
        # The control pass publishes the state on every one of its paths.
        await self._async_control_heating()

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
