"""Fleet-wide switch-on staggering for Generic Water Heater instances.

Why this module exists
----------------------
Every heater instance drives a resistive element of a few kW. When several
instances share one trigger -- for example the same "PV power excess" Smart Eco
template -- Home Assistant fires all of their listeners in a single event-loop
pass, so each instance commands its switch ON within milliseconds of its
siblings. On 2026-08-18 that added ~3.25 kW to an 8000 VA inverter in one step
(two switches commanded 447 ms apart) and contributed to an overload trip.

Spacing those commands apart is what this module does. Since 4.7.0 it also
fits surplus heat to the surplus: see "Surplus fit" below, and read the next
section first, because the two are not the same thing.

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

Surplus fit (4.7.0), and why it is not the budget coming back
------------------------------------------------------------
Heat that runs only because a PV surplus was announced has to fit that surplus,
or it ends it. A surplus detector that judges from the battery sees the heaters'
own draw: on 2026-11-23 two heaters (1.85 kW and 1.19 kW) started 60 s apart on
a surplus that could carry one of them, the pack discharged 300 to 600 W, and
the detector withdrew the surplus from both after 21 minutes. On 2026-11-21 the
same thing took 5 minutes, then 30 minutes of lockout with the array throttled,
then 3 minutes. The stagger does not help: it spaces the starts and still
admits both.

So when a heater is given a surplus POWER reading (optional; without one nothing
here runs), heat that depends on the surplus is admitted one heater at a time,
only if its draw fits what is spare, the tank closest to its goal first; and
when the reading goes short, the heater admitted last gives its heat up first
and the others keep theirs.

What it costs: a heater released within ten minutes of starting waits the full
short-run lockout, twice as long each time it fails again on the same surplus;
a kettle beside two running heaters sends the last one off for fifteen minutes.

What this is not: it never weighs the supply. It holds no budget and no sum of
nameplates, it cannot refuse heat a person, the utility, a template or the
normal thermostat asked for, and it does not shed for an inverter's sake - the
load balancer still does all of that through shed/release, and outranks this.
It decides one question the balancer has no opinion on: which tank gets energy
that is otherwise thrown away. The draw it compares is each heater's own, read
from its own meter while it runs, so there is no second list of nameplates to
drift from the balancer's.

The reading is a lower bound while its source says the array is held back
(`curtailed`): more is available than is being used, by an amount nobody can
measure. There the fit is a probe - admit one, let the reading settle, look
again - and a probe that fails costs one short run and a wait.

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

# --- Surplus fit -----------------------------------------------------------
#
# Heaters sharing one surplus entity all ask in the same event-loop pass. The
# first to ask waits this long so the others' requests are on the table before
# anyone is admitted; without it "closest to its goal first" would mean
# "whichever listener Home Assistant happened to call first".
SURPLUS_ARBITRATION_SECONDS = 2.0
# A heater that yields to a better-placed sibling asks again after this.
SURPLUS_YIELD_SECONDS = 5.0
# A heater that does not fit asks again after this. Short on purpose: a sibling
# further from its goal yields to this one for as long as this one would fit, and
# this one only finds out that it now fits when it asks.
SURPLUS_RETRY_SECONDS = 20.0
# After a heater starts or stops on the surplus, the reading is not trusted for
# this long: the meter behind it polls every 5 to 10 s and the array takes a few
# seconds to follow a load step. Nothing else is admitted or released meanwhile.
SURPLUS_SETTLE_SECONDS = 60.0
# A request not repeated for this long is forgotten (its heater stopped asking).
# Longer than the longest retry above, or a waiting heater would lose its place
# between two of its own retries.
SURPLUS_WANT_TTL_SECONDS = 180.0
# A heater fits when the spare power covers its draw and this much over.
SURPLUS_FIT_MARGIN = 0.10
# The draw assumed for a heater that has not been seen running yet.
SURPLUS_UNKNOWN_DRAW_W = 2000.0
# The reading is "short" below minus this, and a heater is released once it has
# been short this long. Both sit inside what a battery-judged detector tolerates
# before it withdraws the surplus from everyone (500 W for 3 minutes on the
# install this was written for): one release at 60 s and a second at 120 s both
# land before that.
SURPLUS_SHORT_TOLERANCE_W = 150.0
SURPLUS_SHORT_HOLD_SECONDS = 60.0
# With one heater left on the surplus there is nobody to save by going early, so
# it rides out a kettle or a pump: it goes only once the reading has been short
# this long. Still inside the three minutes, so it is this heater's own wait
# that follows and not the detector's verdict on the whole surplus.
SURPLUS_SHORT_HOLD_LAST_SECONDS = 150.0


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


@dataclass(frozen=True)
class SurplusReading:
    """The surplus power as one heater read it.

    ``spare_w`` is what a new load could take without the battery discharging,
    negative for a shortfall, None when there is no usable reading. ``curtailed``
    says the array is held back, so ``spare_w`` is a lower bound.
    """

    spare_w: float | None = None
    curtailed: bool = False


NO_READING = SurplusReading()


@dataclass
class SurplusWant:
    """One heater asking for surplus heat and not yet admitted."""

    name: str
    # How far the tank is from the goal it is ranked by, and whether that goal
    # is only the boost's (the tank already has its normal target). See
    # request_surplus for the order these give.
    distance: float
    extra: bool
    draw_w: float
    first_asked: datetime
    last_asked: datetime

    @property
    def rank(self) -> tuple[bool, float]:
        """Sorts first what should start first."""
        return (self.extra, self.distance)


@dataclass
class SurplusClaim:
    """One heater heating on the surplus.

    ``since`` is when it was admitted, moved to when its element is first seen
    drawing (a stagger hold can sit between the two). ``drawing`` is False until
    then: a claim that never started changed no load when it goes.
    """

    since: datetime
    drawing: bool = False
    # Told to go, relay not yet off (its minimum on-time holds it). It is not
    # picked again, and nobody else is picked while its load is still there.
    releasing: bool = False


def surplus_fits(reading: SurplusReading, draw_w: float | None) -> bool:
    """Whether a heater drawing ``draw_w`` may start on this reading.

    No reading is not a refusal: the surplus was announced by the entity the
    heater has always trusted, and a power sensor that has dropped out must not
    turn a working install into one that never heats. Held back means more is
    there than the number says, so one heater is let in to find out.
    """
    if reading.spare_w is None or reading.curtailed:
        return True
    draw = draw_w if draw_w is not None and draw_w > 0 else SURPLUS_UNKNOWN_DRAW_W
    return reading.spare_w >= draw * (1 + SURPLUS_FIT_MARGIN)


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
        # Surplus fit. Kept apart from the members on purpose: none of it is
        # carried across a reload (a heater found heating on the surplus
        # claims again on sight), and none of it touches the stagger.
        self._surplus_wants: dict[str, SurplusWant] = {}
        self._surplus_claims: dict[str, SurplusClaim] = {}
        self._surplus_changed_at: datetime | None = None
        self._surplus_short_since: datetime | None = None

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
        # A heater that goes takes its place in the surplus with it. Nothing is
        # stamped: whether its element is still drawing is not known here, and
        # it claims again on sight when it comes back.
        self._surplus_wants.pop(entry_id, None)
        self._surplus_claims.pop(entry_id, None)
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

    def holds_state(self, now: datetime) -> bool:
        """Return True while the fleet still has something worth keeping.

        That is a registered member, or a departed stagger anchor that has not
        expired. The caller that owns the fleet's storage must not drop it
        while this is true: when every entry unloads and comes back together,
        the anchors are all that stops the first heater to ask from stepping
        on beside a sibling whose element started seconds ago.
        """
        return bool(self._members) or any(
            anchor.expires_at > now for anchor in self._departed.values()
        )

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
        # A sibling mid-reload (an options save) is briefly unregistered, but
        # its element may have started seconds ago. Its stashed anchor still
        # counts, or a heater asking during that second steps straight on.
        departed = [
            anchor
            for key, anchor in self._departed.items()
            if key != entry_id and anchor.expires_at > now
        ]
        if not siblings and not departed:
            # Single-heater install: nothing to space against.
            self._admit(member, now)
            return ADMIT

        stagger = self.stagger_seconds
        last_sibling_on = max(
            [m.last_admitted for m in siblings if m.last_admitted is not None]
            + [anchor.last_admitted for anchor in departed],
            default=None,
        )
        if stagger > 0 and last_sibling_on is not None:
            elapsed = (now - last_sibling_on).total_seconds()
            if elapsed < 0:
                # The clock stepped back past the sibling's start (an NTP
                # correction, a restored VM). Measured as is, the wait would
                # be the whole step on top of the stagger. Count the window
                # from now instead: one stagger, not an hour.
                for sibling in siblings:
                    if sibling.last_admitted is not None and sibling.last_admitted > now:
                        sibling.last_admitted = now
                for anchor in departed:
                    if anchor.last_admitted > now:
                        anchor.last_admitted = now
                elapsed = 0.0
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
        self._surplus_started(member.entry_id, now)

    # ------------------------------------------------------------------
    # Surplus fit
    # ------------------------------------------------------------------

    def request_surplus(
        self,
        entry_id: str,
        now: datetime,
        *,
        reading: SurplusReading,
        draw_w: float | None,
        distance: float,
        extra: bool = False,
    ) -> FleetDecision:
        """Decide whether ``entry_id`` may start heat that depends on the surplus.

        Asked before ``request_turn_on``, and only for heat that would stop if
        the surplus left. Like the stagger it only ever delays: every refusal
        carries a ``retry_after``.

        THE ORDER (owner, 2026-11-23): the tank closest to its goal first.
        ``distance`` is how far below it the tank is. A surplus is usually too
        small for everyone, and one tank at its target is hot water where two
        tanks halfway there are none; the first cut of this ranked the coldest
        tank first and was changed before it shipped. ``extra`` marks heat
        above the tank's normal target (the boost's): every tank still short of
        its normal target goes before any of that, however close to its boost
        target the other one is.
        """
        member = self._members.get(entry_id)
        if member is None or entry_id in self._surplus_claims:
            # Unregistered: never block heating on bookkeeping we do not have.
            # Already admitted: asking again is free, as with the stagger.
            return ADMIT

        draw = draw_w if draw_w is not None and draw_w > 0 else SURPLUS_UNKNOWN_DRAW_W
        self._prune_surplus_wants(now)
        want = self._surplus_wants.get(entry_id)
        if want is None:
            want = SurplusWant(member.name, distance, extra, draw, now, now)
            self._surplus_wants[entry_id] = want
        want.name, want.distance, want.extra, want.draw_w, want.last_asked = (
            member.name,
            distance,
            extra,
            draw,
            now,
        )

        if self._surplus_changed_at is not None:
            elapsed = (now - self._surplus_changed_at).total_seconds()
            if 0 <= elapsed < SURPLUS_SETTLE_SECONDS:
                return self._surplus_defer(
                    member,
                    SURPLUS_SETTLE_SECONDS - elapsed,
                    "surplus: letting the reading settle after a heater changed",
                    f"{elapsed:.0f}s ago",
                )

        if len(self._members) > 1:
            opened = min(w.first_asked for w in self._surplus_wants.values())
            waited = (now - opened).total_seconds()
            if 0 <= waited < SURPLUS_ARBITRATION_SECONDS:
                return self._surplus_defer(
                    member,
                    SURPLUS_ARBITRATION_SECONDS - waited,
                    "surplus: collecting requests",
                )

        # A sibling closer to its goal goes first, but only one that would
        # itself be admitted: the surplus is not left idle for a heater that
        # does not fit it. Equal ranks fall back to the entry id so the choice
        # is stable; the comparison is strict, so they cannot deadlock.
        rivals = [
            (key, other)
            for key, other in self._surplus_wants.items()
            if key != entry_id
            and (other.rank, key) < (want.rank, entry_id)
            and surplus_fits(reading, other.draw_w)
        ]
        if rivals:
            _, first = min(rivals, key=lambda item: (item[1].rank, item[0]))
            return self._surplus_defer(
                member,
                SURPLUS_YIELD_SECONDS,
                "surplus: a heater closer to its goal goes first",
                f"{first.name}, {first.distance:.1f} C to go against "
                f"{want.distance:.1f} C",
            )

        if not surplus_fits(reading, draw):
            return self._surplus_defer(
                member,
                SURPLUS_RETRY_SECONDS,
                "surplus: not enough to carry this heater",
                f"{reading.spare_w:.0f} W spare, it draws about {draw:.0f} W",
            )

        del self._surplus_wants[entry_id]
        self._surplus_claims[entry_id] = SurplusClaim(since=now)
        # Stamped at once, and again when the element is seen drawing: no
        # sibling is admitted on a reading taken before this one's load shows.
        self._surplus_changed_at = now
        return ADMIT

    def adopt_surplus(self, entry_id: str, now: datetime) -> None:
        """Record a heater found heating on the surplus without having asked.

        After a restart or a reload, or a heater whose heat came to depend on
        the surplus while it ran (the utility left and the surplus stayed).
        Without this a release would find nobody to pick.
        """
        member = self._members.get(entry_id)
        if member is None:
            return
        claim = self._surplus_claims.get(entry_id)
        if claim is not None:
            # Still on and depending on the surplus again (its wait ran out
            # with the relay held on): it is counted like any other.
            claim.releasing = False
            return
        self._surplus_wants.pop(entry_id, None)
        # Dated from when its element last started, where the stagger still
        # knows (it does across a reload), so the order of release and the
        # length of the run survive; otherwise from now.
        started = member.last_admitted
        since = started if started is not None and started <= now else now
        self._surplus_claims[entry_id] = SurplusClaim(since=since, drawing=True)

    def release_surplus(self, entry_id: str, now: datetime) -> None:
        """Give up this heater's surplus heat and any request for it."""
        self._surplus_wants.pop(entry_id, None)
        claim = self._surplus_claims.pop(entry_id, None)
        if claim is not None and claim.drawing:
            # A load is leaving the supply: the reading moves, so let it settle
            # before anyone else is admitted or released on it.
            self._surplus_changed_at = now

    def mark_surplus_releasing(self, entry_id: str) -> None:
        """This heater has been told to go; its relay may stay on for a while.

        The claim stays until the element is really off (release_surplus). A
        release that dropped it at once left a load on the supply that nobody
        was counted for: the reading stayed short and, a settle later, the
        sibling was sent off after it (found in review, with a minimum on-time
        of ten minutes).
        """
        claim = self._surplus_claims.get(entry_id)
        if claim is not None:
            claim.releasing = True

    def surplus_releasing(self, entry_id: str) -> bool:
        claim = self._surplus_claims.get(entry_id)
        return claim is not None and claim.releasing

    def holds_surplus(self, entry_id: str) -> bool:
        """Whether this heater is counted as heating on the surplus."""
        return entry_id in self._surplus_claims

    def surplus_drawing_since(self, entry_id: str) -> datetime | None:
        """When this heater's element started drawing on the surplus, if it has."""
        claim = self._surplus_claims.get(entry_id)
        return claim.since if claim is not None and claim.drawing else None

    def surplus_release_due(
        self, entry_id: str, now: datetime, reading: SurplusReading
    ) -> tuple[bool, float | None]:
        """Whether ``entry_id`` should give up its surplus heat now.

        Returns ``(release, look_again_in)``. The heater admitted last goes
        first, one per settle time, and only once the reading has been short
        for the hold. ``look_again_in`` is set for a heater that is counted and
        has to ask again without waiting for the reading to change (a reading
        that sits at one short value sends no further event).
        """
        if reading.spare_w is None or reading.spare_w >= -SURPLUS_SHORT_TOLERANCE_W:
            self._surplus_short_since = None
            return False, None
        if self._surplus_short_since is None or self._surplus_short_since > now:
            self._surplus_short_since = now
        if entry_id not in self._surplus_claims:
            return False, None

        if any(claim.releasing for claim in self._surplus_claims.values()):
            # One has been told to go and is still drawing. Its load is in the
            # reading; nobody else goes for it.
            return False, SURPLUS_SETTLE_SECONDS
        hold = (
            SURPLUS_SHORT_HOLD_SECONDS
            if len(self._surplus_claims) > 1
            else SURPLUS_SHORT_HOLD_LAST_SECONDS
        )
        due = self._surplus_short_since + timedelta(seconds=hold)
        if self._surplus_changed_at is not None and self._surplus_changed_at <= now:
            due = max(
                due, self._surplus_changed_at + timedelta(seconds=SURPLUS_SETTLE_SECONDS)
            )
        wait = (due - now).total_seconds()
        if wait > 0:
            return False, wait
        last_in = max(
            self._surplus_claims, key=lambda key: (self._surplus_claims[key].since, key)
        )
        if last_in != entry_id:
            # Not this one's turn. If the one whose turn it is does not go (its
            # relay is held on, its entity is busy), look again after a settle.
            return False, SURPLUS_SETTLE_SECONDS
        return True, None

    def _surplus_started(self, entry_id: str, now: datetime) -> None:
        """The element of a heater admitted on the surplus is drawing."""
        claim = self._surplus_claims.get(entry_id)
        if claim is None or claim.drawing:
            return
        claim.drawing = True
        claim.since = now
        self._surplus_changed_at = now

    def _prune_surplus_wants(self, now: datetime) -> None:
        for key in [
            key
            for key, want in self._surplus_wants.items()
            if (now - want.last_asked).total_seconds() > SURPLUS_WANT_TTL_SECONDS
        ]:
            del self._surplus_wants[key]

    @staticmethod
    def _surplus_defer(
        member: FleetMember, retry_after: float, reason: str, detail: str = ""
    ) -> FleetDecision:
        """A refusal. The reason is published as an attribute, so it is one of
        four fixed texts: the numbers behind it change with every reading and
        would write a recorder row a minute for as long as a heater waits. They
        go to the debug log."""
        _LOGGER.debug("fleet: %s deferred -- %s (%s)", member.name, reason, detail)
        return FleetDecision(
            admitted=False, retry_after=max(retry_after, 0.1), reason=reason
        )

    def surplus_snapshot(self) -> dict:
        """The surplus bookkeeping, for diagnostics."""
        return {
            "claims": {
                key: {
                    "since": claim.since,
                    "drawing": claim.drawing,
                    "releasing": claim.releasing,
                }
                for key, claim in self._surplus_claims.items()
            },
            "wants": {
                key: {
                    "distance": want.distance,
                    "extra": want.extra,
                    "draw_w": want.draw_w,
                }
                for key, want in self._surplus_wants.items()
            },
            "changed_at": self._surplus_changed_at,
            "short_since": self._surplus_short_since,
        }

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
        if member is None:
            return
        self._surplus_started(entry_id, now)
        if member.believed_on:
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
