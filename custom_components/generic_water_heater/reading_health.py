"""Reading health: does the tank's reading still follow the tank?

A sensor that stops reporting keeps its last number. Its entity stays
available and its state stays a number, so "no reading, no heat" has nothing
to act on: the heater goes on comparing a target with a temperature nobody has
taken for hours. Seen 2026-11-18: a battery sensor behind a bridge made its
last report at 05:18 UTC, the bridge went on serving that value (and replayed
it after every restart), and from 10:35 the element heated for about 90
minutes on it. Only the eco condition going false stopped it.

Silence alone cannot be the test. A sensor that reports on change is
legitimately silent for half an hour and more while a tank sits still. The
test here is physical: a tank that is being heated moves. `HeatingWatch`
counts the time a tank has been heated since its reading last moved, and the
heater treats the reading as lost once that reaches the limit.

Pure logic, no Home Assistant imports, driven by an injected `now`, like
fleet.py and element_health.py. What "being heated" means (the relay, the
meter, the cold region where no thermostat can be open) is the caller's to
decide; it only tells this when that starts and stops.
"""
from __future__ import annotations

from datetime import datetime, timedelta


# Readings are decimal strings parsed to floats, and 32.3 - 31.8 is
# 0.49999999999999645: without this a move of exactly the band was not one.
_EPSILON = 1e-9


def moved(value: float, baseline: float, band: float) -> bool:
    """Whether `value` is at least `band` from `baseline`, either way."""
    return abs(value - baseline) >= band - _EPSILON


class HeatingWatch:
    """Heated time since the reading last moved.

    "Moved" is at least `band` away from the value the count started on, in
    either direction. Not "differs from the previous reading": two cached
    values served in turn differ every time and move nowhere, and one stray
    report a few hundredths off would start the count again, or release a
    reading already found stale. Not "rose" either: a tank being drawn from
    harder than its element heats falls while it is heated, and that is a live
    sensor.

    The time is banked across pauses and forgotten only when the reading
    moves. A flapping eco condition runs an element three minutes at a time
    (2026-11-18), and a count that started again with each run would never end.
    """

    def __init__(self, limit: timedelta, band: float) -> None:
        self._limit = limit
        self._band = band
        self._baseline: float | None = None
        self._banked = timedelta(0)
        self._heating_since: datetime | None = None

    @property
    def baseline(self) -> float | None:
        """The value the count is measured from, or None before any reading."""
        return self._baseline

    def moved(self, value: float) -> bool:
        """Whether `value` is a move from the baseline (always, with none)."""
        return self._baseline is None or moved(value, self._baseline, self._band)

    def reading(self, now: datetime, value: float) -> bool:
        """Note a reading. True when it moved, which starts the count again.

        A reading that is missing is simply not passed in: it neither counts
        as a move nor clears the baseline, so a sensor that drops out and
        comes back with the same number has not moved.
        """
        if not self.moved(value):
            return False
        self.restart(now, value)
        return True

    def restart(self, now: datetime, value: float | None) -> None:
        """Count from zero, measured from `value` (None: from the next reading)."""
        self._baseline = value
        self._banked = timedelta(0)
        if self._heating_since is not None:
            self._heating_since = now

    def heating(self, now: datetime, on: bool) -> None:
        """Note whether the tank is being heated. Repeating the answer is free."""
        if on:
            if self._heating_since is None:
                self._heating_since = now
        elif self._heating_since is not None:
            self._banked += max(now - self._heating_since, timedelta(0))
            self._heating_since = None

    def heated_for(self, now: datetime) -> timedelta:
        """Heated time since the reading last moved."""
        if self._heating_since is None:
            return self._banked
        return self._banked + max(now - self._heating_since, timedelta(0))

    def due_in(self, now: datetime) -> timedelta | None:
        """How long until the limit at this rate, or None while not counting.

        Not counting: not being heated, or no reading to measure from yet.
        """
        if self._heating_since is None or self._baseline is None:
            return None
        return max(self._limit - self.heated_for(now), timedelta(0))

    def overdue(self, now: datetime) -> bool:
        """Whether the tank has been heated for the limit with nothing to show."""
        return self._baseline is not None and self.heated_for(now) >= self._limit

    def restore(self, value: float, heated_s: float) -> None:
        """Take up a count saved before a restart: `heated_s` seconds from `value`.

        It stands only if the first reading afterwards has not moved from
        `value` (reading() decides that as for any other). So a restart
        cannot buy a stale sensor a fresh count, and a count that is restored
        can stop heat sooner but never start any.

        Seconds, bounded here before they become a timedelta: what is stored
        is read back as any finite number, and a huge one overflowed
        timedelta() and failed the heater's setup (a reviewer's 1e300).
        """
        self._baseline = value
        self._banked = timedelta(
            seconds=min(max(heated_s, 0.0), self._limit.total_seconds())
        )
        self._heating_since = None
