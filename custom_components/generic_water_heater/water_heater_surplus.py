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

    def _forecast_poor(self) -> bool:
        """Whether tomorrow's solar forecast is below the pre-heat threshold.

        An unreadable forecast is not a poor one: pre-heat stores extra heat
        on a guess, so without the forecast it does nothing.
        """
        if self._forecast_preheat is None:
            return False
        state = self.hass.states.get(self._forecast_preheat["entity"])
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return False
        unit = (state.attributes.get("unit_of_measurement") or "kWh").strip()
        kwh = value / 1000 if unit == "Wh" else value * 1000 if unit == "MWh" else value
        return kwh < self._forecast_preheat["min_kwh"]

    def _boost_target_now(self) -> float | None:
        """The boost target in force: raised to the pre-heat target before a poor day."""
        targets = [self._surplus_boost_target] if self._surplus_boost_target is not None else []
        if self._forecast_poor():
            targets.append(self._forecast_preheat["target"])
        return max(targets) if targets else None

    async def _async_forecast_changed(self, _event) -> None:
        await self._async_control_heating()
        self.async_write_ha_state()

    def _surplus_boost_locked(self) -> bool:
        return (
            self._surplus_boost_locked_until is not None
            and dt_util.utcnow() < self._surplus_boost_locked_until
        )

    def _surplus_boost_active(self) -> bool:
        """Whether control is aiming at the boost target right now.

        Only in plain electric: OFF is a person's (or the gate's) and must stay
        reachable, PERFORMANCE already heats without a target, a shed outranks
        everything, and a disinfection cycle runs its own course.
        """
        return (
            self._surplus_boost_configured()
            and self._surplus_boost_enabled()
            and self._surplus_active
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

    def _surplus_boost_heating_now(self) -> bool:
        """Whether the boost is active and its element is on.

        Deliberately coarse: it does not ask whether the tank was above its
        normal target. Below it the eco gate heats on surplus anyway, so a
        lockout changes nothing there. A boost that reached its target and
        stopped before the surplus left reads False: it did not cause the
        surplus to leave.
        """
        if not self._surplus_boost_active():
            return False
        heater = self.hass.states.get(self.heater_entity_id)
        return heater is not None and heater.state == STATE_ON

    def _handle_surplus_edge(self, boost_was_heating: bool) -> None:
        """Open a surplus window, or close one and lock out a short boost."""
        now = dt_util.utcnow()
        if self._surplus_active:
            self._surplus_window_start = now
            self._note_boost_heating(now)
            return

        # Measured from the later of surplus arriving and this run of the
        # element starting. From surplus alone, the first short boost after a
        # long sunny stretch was never locked out -- the window had been open
        # for hours -- and cost one battery-drawing run before the next was
        # (seen 2026-10-18). A run that started before the surplus did is
        # bounded by the surplus window as before.
        started = self._surplus_window_start
        if started is not None and self._boost_heating_since is not None:
            started = max(started, self._boost_heating_since)
        self._surplus_window_start = None
        self._boost_heating_since = None
        if boost_was_heating and started is not None and now - started < SURPLUS_BOOST_SHORT_RUN:
            self._surplus_boost_locked_until = now + SURPLUS_BOOST_LOCKOUT
            if self._surplus_boost_lockout_timer is not None:
                self._surplus_boost_lockout_timer()
            self._surplus_boost_lockout_timer = async_call_later(
                self.hass,
                SURPLUS_BOOST_LOCKOUT.total_seconds(),
                self._async_surplus_boost_lockout_ended,
            )
            self._debug_log(
                "surplus boost: surplus lasted %ss with the element on; locked out until %s",
                int((now - started).total_seconds()),
                self._surplus_boost_locked_until.isoformat(),
            )

    @callback
    def _note_boost_heating(self, now: datetime) -> None:
        """Stamp when the element came on under the boost; clear when it stops."""
        if self._surplus_boost_heating_now():
            if self._boost_heating_since is None:
                self._boost_heating_since = now
        else:
            self._boost_heating_since = None

    async def _async_surplus_boost_lockout_ended(self, _now) -> None:
        """Let the boost try again."""
        self._surplus_boost_lockout_timer = None
        self._surplus_boost_locked_until = None
        self._debug_log("surplus boost: lockout ended")
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
