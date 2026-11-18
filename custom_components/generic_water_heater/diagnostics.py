"""Diagnostics: one download with what is otherwise only in memory.

Options, the fleet's view, the heater's internal state and pending timers --
what the "seen on hardware" items need, without turning on debug logging and
waiting for it to happen again. Nothing here is secret (entity ids and
numbers), so nothing is redacted.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import DOMAIN, async_get_fleet


def _plain(value: Any) -> Any:
    """Make a value JSON-friendly (datetimes as ISO, containers recursed)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one water heater entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    heater = runtime.get("water_heater_entity")
    fleet = async_get_fleet(hass)
    member = fleet.get(entry.entry_id)

    heater_state: dict[str, Any] | None = None
    if heater is not None:
        heater_state = {
            "entity_id": heater.entity_id,
            "available": heater.available,
            "operation": heater._current_operation,
            "current_temperature": heater._current_temperature,
            "target_temperature": heater._target_temperature,
            "effective_target_temperature": heater._effective_target_temperature(),
            "temperature_lost": heater._temperature_lost,
            "awaiting_first_reading": heater._awaiting_first_reading,
            "last_commanded_switch_state": heater._last_commanded_switch_state,
            "pending_switch_state": heater._pending_switch_state,
            "last_switch_change_time": heater._last_switch_change_time,
            "commands_in_flight": heater._commands_in_flight,
            "startup_grace_active": heater._startup_grace_active,
            "load_shed": heater._load_shed,
            "eco_condition_met": heater._eco_condition_met,
            "surplus_active": heater._surplus_active,
            # The carry as held. It counts only while the element reads on.
            "boost_surplus_carried": (
                heater._startup_grace_active and heater._carried_surplus
            ),
            "surplus_window_start": heater._surplus_window_start,
            "off_until_target_for_disinfection": heater._off_until_target_for_disinfection,
            "timers_armed": {
                name: getattr(heater, name) is not None
                for name in (
                    "_cooldown_timer",
                    "_smart_eco_resume_timer",
                    "_surplus_boost_lockout_timer",
                    "_sensor_outage_timer",
                    "_switch_outage_timer",
                    "_failsafe_warn_timer",
                    "_first_reading_timer",
                    "_switch_unavailable_timer",
                    "_manual_performance_timer",
                )
            },
            "attributes": heater.extra_state_attributes,
        }

    return _plain(
        {
            "entry": {
                "title": entry.title,
                "version": entry.version,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "heater": heater_state,
            "fleet": {
                "stagger_seconds": fleet.stagger_seconds,
                "member": asdict(member) if member is not None else None,
            },
            "runtime": {
                key: value
                for key, value in runtime.items()
                if not key.endswith("_entity")
            },
        }
    )
