"""Home Assistant plumbing for the temperature spike filter.

Every platform that reads the tank sensor subscribes through here instead of
``async_track_state_change_event`` directly, so one glitched reading is judged
the same way by all of them. See ``temperature_filter.py`` for the why.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .temperature_filter import SPIKE_CONFIRM, SpikeFilter

# (state value, timestamp, attributes). The value is the raw state string for
# unavailable/unknown/non-numeric states, a float for numeric ones, or None when
# the entity was removed -- the same cases the consumers already distinguish.
TemperatureAction = Callable[[Any, datetime, dict[str, Any]], Any]


@callback
def async_track_filtered_temperature(
    hass: HomeAssistant,
    entity_id: str,
    action: TemperatureAction,
) -> CALLBACK_TYPE:
    """Deliver ``entity_id``'s readings to ``action`` with spikes removed."""
    job = HassJob(action, f"filtered temperature {entity_id}")
    spike_filter = SpikeFilter()
    timer: CALLBACK_TYPE | None = None
    # Attributes of the reading that is pending, so a confirmed reading arrives
    # with its own unit rather than whatever the source carries by then.
    pending_attributes: dict[str, Any] = {}

    @callback
    def _cancel_timer() -> None:
        nonlocal timer
        if timer is not None:
            timer()
            timer = None

    @callback
    def _expired(_now) -> None:
        nonlocal timer
        timer = None
        for reading in spike_filter.expire():
            hass.async_run_hass_job(job, reading.value, reading.when, pending_attributes)

    @callback
    def _changed(event: Event[EventStateChangedData]) -> None:
        nonlocal pending_attributes, timer
        new_state = event.data.get("new_state")
        when = event.time_fired or dt_util.utcnow()

        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Never delayed: the control loop's failsafe acts on these.
            _cancel_timer()
            spike_filter.reset()
            value = None if new_state is None else new_state.state
            attrs = {} if new_state is None else dict(new_state.attributes)
            hass.async_run_hass_job(job, value, when, attrs)
            return

        attributes = dict(new_state.attributes)
        try:
            value = float(new_state.state)
        except (TypeError, ValueError):
            hass.async_run_hass_job(job, new_state.state, when, attributes)
            return

        previous = spike_filter.pending
        readings = spike_filter.add(when, value)
        _cancel_timer()
        for reading in readings:
            # A confirmed pending reading carries the attributes captured when
            # it arrived, not this one's.
            attrs = pending_attributes if reading is previous else attributes
            hass.async_run_hass_job(job, reading.value, reading.when, attrs)
        if spike_filter.pending is not None:
            pending_attributes = attributes
            timer = async_call_later(hass, SPIKE_CONFIRM.total_seconds(), _expired)

    if (current := hass.states.get(entity_id)) is not None:
        try:
            spike_filter.prime(float(current.state))
        except (TypeError, ValueError):
            pass

    unsub_state = async_track_state_change_event(hass, [entity_id], _changed)

    @callback
    def _unsubscribe() -> None:
        _cancel_timer()
        unsub_state()

    return _unsubscribe
