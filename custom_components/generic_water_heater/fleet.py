"""Fleet-wide electrical admission control for Generic Water Heater instances.

Why this module exists
----------------------
Every heater instance drives a resistive element of a few kW. When several
instances share one trigger -- for example the same "PV power excess" Smart Eco
template -- Home Assistant fires all of their listeners in a single event-loop
pass, so each instance commands its switch ON within milliseconds of its
siblings. On 2026-08-18 that added ~3.25 kW to an 8000 VA inverter in one step
(two switches commanded 447 ms apart) and contributed to an overload trip.

Admission here is based on NAMEPLATE watts, never on power telemetry: the house
power sensors poll every 16-62 s, far too slow to gate a decision that has to be
made in the same event-loop pass that requested it. Nameplate accounting has
zero latency, which is the whole point.

This module deliberately imports nothing from Home Assistant. It is pure
decision logic driven by an injected ``now``, so it can be reasoned about and
unit tested without a running Home Assistant. All the Home Assistant plumbing
(timers, ``hass.data`` storage, service calls) lives in ``water_heater.py``.

Invariants worth knowing
------------------------
* The budget only ever prevents *aggregate* overload. A heater is never blocked
  when no sibling holds any watts, so a budget smaller than a single heater's
  nameplate cannot leave the house permanently without hot water.
* Every refusal carries a ``retry_after``. The caller re-queues through the
  existing cooldown-timer path, so no request is ever dropped -- only delayed.
* A member with no ``nominal_power_w`` counts as 0 W and is therefore invisible
  to the budget. Registration logs a warning when that combination is seen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

_LOGGER = logging.getLogger(__name__)

# Reserved key inside hass.data[DOMAIN]. Config entry ids are 26-character
# ULIDs, so this cannot collide with one.
FLEET_KEY = "_fleet"

DEFAULT_NOMINAL_POWER_W = 0.0
DEFAULT_STAGGER_SECONDS = 60.0
DEFAULT_BUDGET_W = 0.0

# How long a budget-blocked request waits before re-checking on its own. This is
# only a backstop: a release wakes the best waiter immediately.
BUDGET_RETRY_SECONDS = 60.0

# How long a request yields for when a higher-priority sibling is waiting.
PRIORITY_YIELD_SECONDS = 5.0

# How long the first request in a contended fleet is held so that siblings
# triggered in the same event-loop pass can be ranked by temperature deficit
# rather than by arrival order.
ARBITRATION_SECONDS = 1.0

# A deferred member stops counting as a waiter if it has not asked again within
# this window, so a heater that quietly reached target cannot block siblings.
WAITER_TTL_SECONDS = 300.0

# Commitments are booked on intent, because waiting for a switch to confirm
# would reintroduce exactly the latency this module exists to avoid. That makes
# every commitment a claim about the world which has to be reconciled against
# it, or a heater that never actually switched on would starve its siblings for
# ever. Three bounds, each the point past which the claim is not credible:
#
#   * a commanded switch that has not reported ON is probably not on,
#   * a switch that has been unavailable this long is probably unpowered,
#   * an unloaded entry whose element may still be energised, long enough to
#     cover a config entry reload.
UNCONFIRMED_COMMITMENT_SECONDS = 120.0
UNAVAILABLE_COMMITMENT_SECONDS = 600.0
DEPARTED_COMMITMENT_SECONDS = 60.0


@dataclass
class DepartedCommitment:
    """Watts of an unloaded entry whose element may still be energised.

    Unloading a config entry does not switch its heater off, so the watts must
    not be handed to a sibling the instant the entry disappears -- an options
    save reloads the entry, and that is precisely when both elements could end
    up on together.
    """

    entry_id: str
    nominal_power_w: float
    expires_at: datetime


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
    nominal_power_w: float = DEFAULT_NOMINAL_POWER_W
    stagger_seconds: float = DEFAULT_STAGGER_SECONDS
    budget_w: float = DEFAULT_BUDGET_W
    wake: Callable[[], None] | None = None

    # Runtime state.
    committed: bool = False
    # True once the switch has actually been observed ON. An unconfirmed
    # commitment is only a claim that a command was sent.
    confirmed: bool = False
    # When an unverified commitment stops being credible. None means the switch
    # is observed ON, which needs no expiry.
    expires_at: datetime | None = None
    last_admitted: datetime | None = None
    waiting_since: datetime | None = None
    last_request: datetime | None = None
    last_woken: datetime | None = None
    deficit: float = 0.0


class HeaterFleet:
    """Coordinates switch-on requests across every water heater instance.

    One instance of this is shared by all config entries and survives the
    unload of any individual entry.
    """

    def __init__(self) -> None:
        """Initialize an empty fleet."""
        self._members: dict[str, FleetMember] = {}
        self._departed: dict[str, DepartedCommitment] = {}

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def register(
        self,
        entry_id: str,
        name: str,
        *,
        nominal_power_w: float | None = DEFAULT_NOMINAL_POWER_W,
        stagger_seconds: float | None = DEFAULT_STAGGER_SECONDS,
        budget_w: float | None = DEFAULT_BUDGET_W,
        wake: Callable[[], None] | None = None,
    ) -> FleetMember:
        """Add or update a member. Runtime state is preserved on re-register."""
        member = self._members.get(entry_id)
        if member is None:
            member = FleetMember(entry_id=entry_id, name=name)
            self._members[entry_id] = member

        member.name = name
        member.nominal_power_w = _non_negative(nominal_power_w, DEFAULT_NOMINAL_POWER_W)
        member.stagger_seconds = _non_negative(stagger_seconds, DEFAULT_STAGGER_SECONDS)
        member.budget_w = _non_negative(budget_w, DEFAULT_BUDGET_W)
        member.wake = wake

        # The entry is back (a reload). Its own held watts belong to it again.
        self._departed.pop(entry_id, None)

        if self.budget_w > 0 and member.nominal_power_w <= 0:
            _LOGGER.warning(
                "%s: a fleet power budget of %.0f W is configured, but this heater has no "
                "nominal power set. It will draw real watts that the budget cannot see. "
                "Set its nameplate wattage in the integration options",
                name,
                self.budget_w,
            )

        return member

    def unregister(self, entry_id: str, now: datetime | None = None) -> None:
        """Remove a member; its committed watts leave the pool with it."""
        member = self._members.pop(entry_id, None)
        if member is None:
            return

        if member.committed and member.nominal_power_w > 0 and now is not None:
            # Unloading an entry does not switch its heater off. Hold the watts
            # for a grace period rather than handing them straight to a sibling,
            # and deliberately do NOT wake anyone: a waiting sibling retries on
            # its own timer, by which time a reloading entry has re-registered
            # and re-booked what it is really drawing.
            self._departed[entry_id] = DepartedCommitment(
                entry_id=entry_id,
                nominal_power_w=member.nominal_power_w,
                expires_at=now + timedelta(seconds=DEPARTED_COMMITMENT_SECONDS),
            )
            _LOGGER.debug(
                "fleet: %s unregistered while holding %.0f W; holding it for %.0fs "
                "in case the element is still energised",
                member.name,
                member.nominal_power_w,
                DEPARTED_COMMITMENT_SECONDS,
            )

    def get(self, entry_id: str) -> FleetMember | None:
        """Return a member, or None when it is not registered."""
        return self._members.get(entry_id)

    @property
    def is_empty(self) -> bool:
        """Return True when no member is registered."""
        return not self._members

    # ------------------------------------------------------------------
    # Resolved fleet-wide settings
    #
    # Each entry carries its own copy of the fleet settings, so the fleet
    # resolves them to the most conservative value any member asks for. That
    # is deterministic regardless of the order entries happen to load in.
    # ------------------------------------------------------------------

    @property
    def stagger_seconds(self) -> float:
        """Return the longest stagger any member asks for."""
        return max(
            (member.stagger_seconds for member in self._members.values()),
            default=DEFAULT_STAGGER_SECONDS,
        )

    @property
    def budget_w(self) -> float:
        """Return the tightest budget any member asks for; 0 means unlimited."""
        budgets = [m.budget_w for m in self._members.values() if m.budget_w > 0]
        return min(budgets) if budgets else 0.0

    @property
    def committed_power_w(self) -> float:
        """Return the nameplate watts currently committed across the fleet.

        Includes watts still held for entries that have unloaded but whose
        elements may not have switched off. Expired claims are dropped by
        _prune_commitments on the next request.
        """
        return sum(
            m.nominal_power_w for m in self._members.values() if m.committed
        ) + sum(d.nominal_power_w for d in self._departed.values())

    def committed_power_w_excluding(self, entry_id: str) -> float:
        """Return committed watts held by every member except ``entry_id``."""
        return sum(
            m.nominal_power_w
            for m in self._members.values()
            if m.committed and m.entry_id != entry_id
        ) + sum(
            d.nominal_power_w
            for d in self._departed.values()
            if d.entry_id != entry_id
        )

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    def request_turn_on(
        self, entry_id: str, now: datetime, deficit: float = 0.0
    ) -> FleetDecision:
        """Decide whether ``entry_id`` may command its switch ON right now.

        ``deficit`` is the temperature shortfall (target - current) in the
        heater's own unit, used to rank contending heaters. A refusal always
        carries a ``retry_after`` for the caller to re-queue on.
        """
        member = self._members.get(entry_id)
        if member is None:
            # Not registered (platform still setting up). Never block heating on
            # bookkeeping we do not have.
            return ADMIT

        member.deficit = float(deficit or 0.0)
        member.last_request = now

        if member.committed:
            # Already holds its share of the budget; re-affirming is free.
            member.waiting_since = None
            return ADMIT

        siblings = [m for m in self._members.values() if m.entry_id != entry_id]
        if not siblings and not self._departed:
            # Single-heater install: nothing to coordinate with. Watts still held
            # for a departed entry count, or an entry reloading while its element
            # is energised would look like a single-heater install and wave the
            # sibling straight through.
            self._admit(member, now)
            return ADMIT

        # Reconcile before deciding: drop commitments that are no longer
        # credible, and waiters that stopped asking. Never wake the requester
        # itself -- it is already here, mid-request.
        self._prune_commitments(now, exclude_wake=entry_id)
        self._prune_stale_waiters(now)

        # 1. Arbitration window. Only applies when the budget genuinely cannot
        #    fit every heater at once -- otherwise the stagger alone is enough
        #    and there is no reason to delay the first request.
        #
        #    The window is anchored on the first request of the burst rather
        #    than on each heater's own arrival, so siblings that reach us a few
        #    hundred milliseconds apart are still ranked at a single instant.
        if self._contention_possible():
            if member.waiting_since is None:
                member.waiting_since = now
            anchor = min(
                (
                    m.waiting_since
                    for m in self._members.values()
                    if m.waiting_since is not None
                ),
                default=now,
            )
            waited = (now - anchor).total_seconds()
            if waited < ARBITRATION_SECONDS:
                return self._defer(
                    member, now, ARBITRATION_SECONDS - waited, "arbitration window"
                )

        # 2. Stagger. No instance may follow a sibling's ON command inside the
        #    stagger window. This is the primary defence against the whole fleet
        #    stepping on in one event-loop pass.
        stagger = self.stagger_seconds
        last_sibling_on = max(
            (m.last_admitted for m in siblings if m.last_admitted is not None),
            default=None,
        )
        if stagger > 0 and last_sibling_on is not None:
            elapsed = (now - last_sibling_on).total_seconds()
            if elapsed < stagger:
                return self._defer(
                    member,
                    now,
                    stagger - elapsed,
                    f"stagger: a sibling switched on {elapsed:.1f}s ago "
                    f"(minimum spacing {stagger:.0f}s)",
                )

        # 3. Nameplate budget. Skipped entirely when no sibling holds watts, so
        #    the budget can only ever prevent an aggregate step -- never leave
        #    the house with no hot water at all.
        budget = self.budget_w
        others_w = self.committed_power_w_excluding(entry_id)
        if budget > 0 and others_w > 0:
            projected = others_w + member.nominal_power_w
            if projected > budget:
                return self._defer(
                    member,
                    now,
                    BUDGET_RETRY_SECONDS,
                    f"budget: {others_w:.0f} W already committed + {member.nominal_power_w:.0f} W "
                    f"requested would reach {projected:.0f} W, over the {budget:.0f} W budget",
                )

        # 4. Priority. Yield to a sibling that is already waiting with a larger
        #    temperature deficit -- but only when the free capacity would
        #    actually admit that sibling, so capacity is never idled for a
        #    request that cannot be served anyway.
        free_w = (budget - others_w) if budget > 0 else None
        winner = self._best_waiter(
            now, exclude=entry_id, above_deficit=member.deficit, free_w=free_w
        )
        if winner is not None:
            self._wake(winner, now)
            return self._defer(
                member,
                now,
                PRIORITY_YIELD_SECONDS,
                f"priority: {winner.name} is waiting with a larger deficit "
                f"({winner.deficit:.1f} vs {member.deficit:.1f})",
            )

        self._admit(member, now)
        _LOGGER.debug(
            "fleet: admitted %s (%.0f W); committed now %.0f W",
            member.name,
            member.nominal_power_w,
            self.committed_power_w,
        )
        return ADMIT

    def release(self, entry_id: str, now: datetime | None = None) -> None:
        """Give back a member's committed watts and wake the best waiter."""
        member = self._members.get(entry_id)
        if member is None:
            return

        was_committed = member.committed
        member.committed = False
        member.confirmed = False
        member.expires_at = None
        member.waiting_since = None

        if was_committed:
            _LOGGER.debug(
                "fleet: released %s (%.0f W); committed now %.0f W",
                member.name,
                member.nominal_power_w,
                self.committed_power_w,
            )
            self._wake_best_waiter(now)

    def note_switch_state(self, entry_id: str, is_on: bool, now: datetime) -> None:
        """Reconcile bookkeeping against the switch's observed state.

        A manual or externally driven ON still draws real watts, so it is booked
        against the budget even though it was never admitted. An observed OFF
        releases whatever the member was holding.
        """
        member = self._members.get(entry_id)
        if member is None:
            return

        if is_on:
            if not member.committed:
                _LOGGER.debug(
                    "fleet: %s observed ON without admission (manual or external); "
                    "booking %.0f W",
                    member.name,
                    member.nominal_power_w,
                )
                member.committed = True
                member.last_admitted = now
                member.waiting_since = None
            # Observed ON is ground truth: the claim needs no expiry now.
            member.confirmed = True
            member.expires_at = None
            self._departed.pop(entry_id, None)
            return

        self.release(entry_id, now)

    def note_switch_unavailable(self, entry_id: str, now: datetime) -> None:
        """Handle a switch that stopped reporting while holding watts.

        An unavailable switch is not released immediately: it may well still be
        drawing, and assuming otherwise would let a sibling double up. But it is
        not held for ever either -- the far likelier cause is that the element
        lost power, and an unbounded hold would starve every sibling with no way
        back short of a reload.
        """
        member = self._members.get(entry_id)
        if member is None or not member.committed:
            return

        deadline = now + timedelta(seconds=UNAVAILABLE_COMMITMENT_SECONDS)
        if member.expires_at is None or deadline < member.expires_at:
            member.expires_at = deadline
            _LOGGER.debug(
                "fleet: %s went unavailable holding %.0f W; keeping the claim for %.0fs",
                member.name,
                member.nominal_power_w,
                UNAVAILABLE_COMMITMENT_SECONDS,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _contention_possible(self) -> bool:
        """Return True when the budget cannot fit every member at once."""
        budget = self.budget_w
        if budget <= 0:
            return False
        return sum(m.nominal_power_w for m in self._members.values()) > budget

    def _admit(self, member: FleetMember, now: datetime) -> None:
        """Book a member's watts and start its stagger window.

        The claim is unconfirmed until the switch is actually observed ON, and
        expires if that never happens -- otherwise a dead relay would hold the
        budget for ever.
        """
        member.committed = True
        member.confirmed = False
        member.expires_at = now + timedelta(seconds=UNCONFIRMED_COMMITMENT_SECONDS)
        member.last_admitted = now
        member.waiting_since = None

    def _prune_commitments(self, now: datetime, exclude_wake: str | None = None) -> None:
        """Release commitments that are no longer credible, and wake a waiter."""
        freed = False

        for member in self._members.values():
            if not member.committed or member.expires_at is None:
                continue
            if now < member.expires_at:
                continue
            _LOGGER.warning(
                "%s: releasing its %.0f W claim on the fleet power budget -- the switch "
                "has not been seen ON since it was commanded. If the heater really is "
                "running, its power is now unaccounted for; if it is not, siblings were "
                "being held back by it",
                member.name,
                member.nominal_power_w,
            )
            member.committed = False
            member.confirmed = False
            member.expires_at = None
            freed = True

        for entry_id, departed in list(self._departed.items()):
            if now >= departed.expires_at:
                del self._departed[entry_id]
                freed = True

        if freed:
            self._wake_best_waiter(now, exclude=exclude_wake)

    def _defer(
        self, member: FleetMember, now: datetime, retry_after: float, reason: str
    ) -> FleetDecision:
        """Refuse a request, remembering the member as an active waiter."""
        if member.waiting_since is None:
            member.waiting_since = now
        _LOGGER.debug("fleet: deferring %s -- %s", member.name, reason)
        return FleetDecision(admitted=False, retry_after=max(retry_after, 0.0), reason=reason)

    def _waiter_ttl(self) -> float:
        """Return how long a deferred member keeps counting as a waiter."""
        return max(WAITER_TTL_SECONDS, self.stagger_seconds * 2, BUDGET_RETRY_SECONDS * 2)

    def _prune_stale_waiters(self, now: datetime) -> None:
        """Forget waiters that stopped asking (reached target, turned off...)."""
        ttl = self._waiter_ttl()
        for member in self._members.values():
            if member.committed or member.waiting_since is None:
                continue
            last = member.last_request or member.waiting_since
            if (now - last).total_seconds() > ttl:
                member.waiting_since = None

    def _active_waiters(self, now: datetime) -> list[FleetMember]:
        """Return members still actively asking for capacity."""
        self._prune_stale_waiters(now)
        return [
            member
            for member in self._members.values()
            if not member.committed and member.waiting_since is not None
        ]

    def _best_waiter(
        self,
        now: datetime,
        *,
        exclude: str | None = None,
        above_deficit: float | None = None,
        free_w: float | None = None,
    ) -> FleetMember | None:
        """Return the highest-deficit waiter matching the given constraints."""
        candidates = [m for m in self._active_waiters(now) if m.entry_id != exclude]

        if above_deficit is not None:
            candidates = [m for m in candidates if m.deficit > above_deficit]
        if free_w is not None:
            candidates = [m for m in candidates if m.nominal_power_w <= free_w]

        if not candidates:
            return None

        # Ties fall back to entry_id so the choice is stable, never arbitrary.
        return max(candidates, key=lambda m: (m.deficit, m.entry_id))

    def _wake_best_waiter(
        self, now: datetime | None = None, exclude: str | None = None
    ) -> None:
        """Wake the highest-deficit waiter that the freed capacity can admit."""
        if now is None:
            # A release is driven by a control pass that has no clock of its own
            # to hand us. The newest request timestamp is close enough for the
            # staleness check and keeps this module free of a wall clock.
            now = _latest_request(self._members.values())
        if now is None:
            return

        budget = self.budget_w
        free_w = (budget - self.committed_power_w) if budget > 0 else None
        winner = self._best_waiter(now, exclude=exclude, free_w=free_w)
        if winner is not None:
            self._wake(winner, now)

    def _wake(self, member: FleetMember, now: datetime | None = None) -> None:
        """Ask a member to re-evaluate immediately.

        A single request can both reconcile a stale claim and yield on priority,
        and both want the same member to run. One wake is enough.
        """
        if member.wake is None:
            return
        if now is not None:
            if member.last_woken == now:
                return
            member.last_woken = now
        _LOGGER.debug("fleet: waking %s to re-evaluate", member.name)
        try:
            member.wake()
        except Exception:  # pragma: no cover - a wake must never break a release
            _LOGGER.exception("fleet: wake callback for %s failed", member.name)


def _non_negative(value: float | None, default: float) -> float:
    """Coerce a config value to a non-negative float, falling back to default."""
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _latest_request(members) -> datetime | None:
    """Return the most recent request time seen across members."""
    stamps = [m.last_request for m in members if m.last_request is not None]
    return max(stamps) if stamps else None
