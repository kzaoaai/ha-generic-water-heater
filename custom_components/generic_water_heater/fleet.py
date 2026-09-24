"""Fleet-wide switch-on staggering for Generic Water Heater instances.

Why this module exists
----------------------
Every heater instance drives a resistive element of a few kW. When several
instances share one trigger -- for example the same "PV power excess" Smart Eco
template -- Home Assistant fires all of their listeners in a single event-loop
pass, so each instance commands its switch ON within milliseconds of its
siblings. On 2026-08-18 that added ~3.25 kW to an 8000 VA inverter in one step
(two switches commanded 447 ms apart) and contributed to an overload trip.

Spacing those commands apart is what this module does, and now all it does.

What used to live here, and why it is gone
------------------------------------------
Until 2.0.0 this module also ran nameplate-watt admission control: a committed
watts pool, reconciliation of those commitments against observed switch state,
a departed-commitment grace period so an entry reload could not hand a sibling
watts that were still energised, and priority arbitration that ranked
contending heaters by temperature deficit.

That was a second answer to a question the Power Load Balancer integration now
owns outright -- including the zero-latency part that justified nameplate
accounting in the first place, since it carries its own per-appliance nameplate
and can veto a turn-on before any power sensor could react. Two systems
balancing one inverter from two independently-maintained sets of nameplates is
worse than one: they disagree silently, and the one that cannot see the rest of
the house is the one that should give way.

So the watts left and the spacing stayed. The spacing is not redundant with a
balancer: it addresses the instant at which two elements step on together,
which is a sequencing problem rather than a budget one, and it needs no
nameplate to do its job.

This module deliberately imports nothing from Home Assistant. It is pure
decision logic driven by an injected ``now``, so it can be reasoned about and
unit tested without a running Home Assistant. All the Home Assistant plumbing
(timers, ``hass.data`` storage, service calls) lives in ``water_heater.py``.

Invariants worth knowing
------------------------
* A heater is only ever *delayed*, never refused outright. Every refusal
  carries a ``retry_after`` and the caller re-queues through the existing
  cooldown-timer path, so no request is dropped.
* A single-heater install is never gated: with no sibling there is nothing to
  space against.
* The window opens when an element STARTS DRAWING, however that happened --
  admitted here, flipped by a person, or found already on after a restart. An
  ON this integration did not command energises the same inverter.
* Re-asking while an element is already believed on never pushes the window
  forward, or a heater whose switch stops reporting would starve its sibling by
  re-asking on every control pass.
* The window survives the unregister/register pair an options save triggers.
  That reload is exactly when two elements could otherwise come on together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

_LOGGER = logging.getLogger(__name__)

# Reserved key inside hass.data[DOMAIN]. Config entry ids are 26-character
# ULIDs, so this cannot collide with one.
FLEET_KEY = "_fleet"

DEFAULT_STAGGER_SECONDS = 60.0

# An unregistered entry's stagger anchor is remembered this long, so the reload
# an options save triggers cannot clear the window. Comfortably longer than the
# largest stagger the options form allows (3600 s), because an anchor older than
# the window in force is harmless -- the elapsed check admits anyway.
DEPARTED_ANCHOR_SECONDS = 7200.0


@dataclass(frozen=True)
class FleetDecision:
    """Outcome of an admission request."""

    admitted: bool
    retry_after: float = 0.0
    reason: str = ""

    def __bool__(self) -> bool:
        """Allow ``if decision:`` to read as "was it admitted"."""
        return self.admitted


ADMIT = FleetDecision(admitted=True)


@dataclass
class FleetMember:
    """One water heater instance as the fleet sees it."""

    entry_id: str
    name: str
    stagger_seconds: float = DEFAULT_STAGGER_SECONDS

    # Runtime state: when this member's element last started drawing. This is
    # the only thing the stagger measures from.
    last_admitted: datetime | None = None
    # Whether the element is believed to be drawing right now. Its job is to
    # make re-asking idempotent: without it, a heater whose switch stops
    # reporting re-asks on every control pass and walks its own anchor forward,
    # starving the sibling indefinitely.
    believed_on: bool = False


@dataclass
class DepartedAnchor:
    """One unregistered entry's stagger state, held across a reload.

    ``believed_on`` travels with the anchor so that a reload does not look like
    a fresh switch-on and re-date the window: the element never stopped, and
    the anchor records when it STARTED. Carrying it is safe because the entity
    corrects it on the way back up -- it reports the switch's real state, which
    clears the belief if the element did stop meanwhile, and leaves it alone if
    the switch cannot be read at all (in which case it probably is still on).
    """

    last_admitted: datetime
    believed_on: bool
    expires_at: datetime


class HeaterFleet:
    """Spaces switch-on commands across every water heater instance.

    One instance of this is shared by all config entries and survives the
    unload of any individual entry.
    """

    def __init__(self) -> None:
        """Initialize an empty fleet."""
        self._members: dict[str, FleetMember] = {}
        self._departed: dict[str, DepartedAnchor] = {}

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def register(
        self,
        entry_id: str,
        name: str,
        *,
        stagger_seconds: float | None = DEFAULT_STAGGER_SECONDS,
    ) -> FleetMember:
        """Add or update a member, recovering the anchor a reload left behind.

        A reload is unregister THEN register, so preserving state on the member
        object alone would be dead code for the path that matters: the entity is
        removed first and the member goes with it.
        """
        member = self._members.get(entry_id)
        if member is None:
            member = FleetMember(entry_id=entry_id, name=name)
            self._members[entry_id] = member
            stashed = self._departed.pop(entry_id, None)
            if stashed is not None:
                member.last_admitted = stashed.last_admitted
                member.believed_on = stashed.believed_on
                _LOGGER.debug(
                    "fleet: %s re-registered; stagger window carried over from "
                    "before the reload",
                    name,
                )

        member.name = name
        member.stagger_seconds = _non_negative(stagger_seconds, DEFAULT_STAGGER_SECONDS)

        return member

    def unregister(self, entry_id: str, now: datetime | None = None) -> None:
        """Remove a member, holding on to its stagger anchor for a reload."""
        member = self._members.pop(entry_id, None)
        if member is None or member.last_admitted is None or now is None:
            return

        self._departed[entry_id] = DepartedAnchor(
            last_admitted=member.last_admitted,
            believed_on=member.believed_on,
            expires_at=now + timedelta(seconds=DEPARTED_ANCHOR_SECONDS),
        )
        # Opportunistic hygiene: an entry deleted for good would otherwise leave
        # its anchor behind for the life of the process.
        for stale in [
            key for key, anchor in self._departed.items() if anchor.expires_at <= now
        ]:
            del self._departed[stale]

    def get(self, entry_id: str) -> FleetMember | None:
        """Return a member, or None when it is not registered."""
        return self._members.get(entry_id)

    @property
    def is_empty(self) -> bool:
        """Return True when no member is registered."""
        return not self._members

    @property
    def stagger_seconds(self) -> float:
        """Return the longest stagger any member asks for.

        Each entry carries its own copy of the setting, so the fleet resolves
        it to the most conservative value any member asks for. That is
        deterministic regardless of the order entries happen to load in.
        """
        return max(
            (member.stagger_seconds for member in self._members.values()),
            default=DEFAULT_STAGGER_SECONDS,
        )

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    def request_turn_on(self, entry_id: str, now: datetime) -> FleetDecision:
        """Decide whether ``entry_id`` may command its switch ON right now.

        A refusal always carries a ``retry_after`` for the caller to re-queue
        on. Callers must not ask on behalf of a heater that is already drawing:
        there is nothing to admit, and asking would report a spurious hold.
        """
        member = self._members.get(entry_id)
        if member is None:
            # Not registered (platform still setting up). Never block heating on
            # bookkeeping we do not have.
            return ADMIT

        if member.believed_on:
            # Already drawing; re-affirming is free and must NOT re-anchor.
            return ADMIT

        siblings = [m for m in self._members.values() if m.entry_id != entry_id]
        if not siblings:
            # Single-heater install: nothing to space against.
            self._admit(member, now)
            return ADMIT

        stagger = self.stagger_seconds
        last_sibling_on = max(
            (m.last_admitted for m in siblings if m.last_admitted is not None),
            default=None,
        )
        if stagger > 0 and last_sibling_on is not None:
            elapsed = (now - last_sibling_on).total_seconds()
            if elapsed < stagger:
                retry_after = stagger - elapsed
                reason = (
                    f"stagger: a sibling switched on {elapsed:.1f}s ago "
                    f"(minimum spacing {stagger:.0f}s)"
                )
                _LOGGER.debug("fleet: %s deferred -- %s", member.name, reason)
                return FleetDecision(
                    admitted=False, retry_after=retry_after, reason=reason
                )

        self._admit(member, now)
        return ADMIT

    def _admit(self, member: FleetMember, now: datetime) -> None:
        """Clear a member to switch on, and start its stagger window."""
        member.last_admitted = now
        member.believed_on = True

    # ------------------------------------------------------------------
    # Observed switch state
    # ------------------------------------------------------------------

    def note_switch_on(self, entry_id: str, now: datetime) -> None:
        """Record that this member's element is drawing.

        Called for an ON this integration did not command -- a person at the
        wall, another automation, or a switch discovered already on after a
        restart -- and also defensively before asking, for a switch that is on
        while the fleet has no record of it. Such an element loads the same
        inverter, so it has to open the window; a sibling admitted on top of it
        is the 2026-08-18 step.

        Only the FIRST observation anchors. Repeated reports of an unchanged ON
        must not walk the window forward.
        """
        member = self._members.get(entry_id)
        if member is None or member.believed_on:
            return

        _LOGGER.debug(
            "fleet: %s observed ON without admission; opening the stagger window",
            member.name,
        )
        member.believed_on = True
        member.last_admitted = now

    def note_switch_off(self, entry_id: str, now: datetime | None = None) -> None:
        """Record that this member's element has stopped drawing.

        The anchor is deliberately left where it is: it is a record of when the
        element last started, which is what the sibling's spacing is measured
        against, and that does not become untrue when the element goes off.
        """
        member = self._members.get(entry_id)
        if member is not None:
            member.believed_on = False


def _non_negative(value: float | None, default: float) -> float:
    """Coerce a config value to a non-negative float, falling back to default."""
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default
