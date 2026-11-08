"""Today's element energy, split by what was carrying the house at the time.

Three buckets, from the inputs Smart Eco already has:

- surplus: the surplus entity was active, so the element ran on solar that
  would otherwise have gone unused;
- utility: no surplus, but the utility entity was active (the grid);
- other: neither -- battery, generator, or an input that could not be read.

It is an attribution, not a measurement: while surplus is active the whole
draw is counted as solar, even if the surplus was smaller than the element.

Integrated from the meter (`power_sensor`): each interval is credited at the
power and source in force at its start, and an interval longer than MAX_GAP
(Home Assistant was stopped, say) is not credited at all. Unobserved time is
not evidence of anything. The buckets reset at local midnight; an interval
spanning it is split there.

Pure logic, no Home Assistant imports, like fleet.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

SOURCE_SURPLUS = "surplus"
SOURCE_UTILITY = "utility"
SOURCE_OTHER = "other"
SOURCES = (SOURCE_SURPLUS, SOURCE_UTILITY, SOURCE_OTHER)

MAX_GAP = timedelta(minutes=10)


def source_for(surplus_active: bool, utility_active: bool) -> str:
    if surplus_active:
        return SOURCE_SURPLUS
    if utility_active:
        return SOURCE_UTILITY
    return SOURCE_OTHER


class EnergySplit:
    """kWh per source for one local day."""

    def __init__(self, day: date) -> None:
        self.day = day
        self.kwh = {source: 0.0 for source in SOURCES}
        self._last: tuple[datetime, float, str] | None = None

    def restore(self, source: str, day: date, kwh: float) -> None:
        """Add a value saved before a restart, if it is from the same day."""
        if day == self.day and source in self.kwh and kwh > 0:
            self.kwh[source] += kwh

    def advance(self, now: datetime, power_w: float | None, source: str) -> None:
        """Credit the interval since the last call, then note what holds now.

        ``now`` must be timezone-aware local time: the day boundary is local
        midnight.
        """
        if self._last is not None:
            start, watts, was = self._last
            if now > start and now - start <= MAX_GAP:
                midnight = datetime.combine(
                    start.date() + timedelta(days=1), datetime.min.time(), start.tzinfo
                )
                if now.date() != start.date():
                    self._credit(was, watts, midnight - start)
                    self._reset(now.date())
                    start = midnight
                self._credit(was, watts, now - start)
        if now.date() != self.day:
            self._reset(now.date())
        self._last = (now, power_w if power_w and power_w > 0 else 0.0, source)

    def _credit(self, source: str, watts: float, span: timedelta) -> None:
        if watts > 0 and span > timedelta(0):
            self.kwh[source] += watts * span.total_seconds() / 3_600_000

    def _reset(self, day: date) -> None:
        self.day = day
        self.kwh = {source: 0.0 for source in SOURCES}
