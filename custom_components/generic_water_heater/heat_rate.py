"""Heat-up rate: how fast this tank warms while its element draws.

Learned from the tank's own heating runs, so it carries the element's power,
the tank's volume, and wherever the sensor sits up the tank, without anyone
having to enter any of them. It feeds a "Ready At" estimate: at 45 C heading
for 53 C with a learned 8.5 C/h, hot water is about 56 minutes away.

A run is one stretch of the element drawing, from the first reading while it
draws to the last. Only runs long enough and warming enough to measure are
learned from; hot water drawn mid-run makes a run read slow, which the moving
average absorbs rather than rejects -- the tank in use is the tank people wait
for.

Pure logic, no Home Assistant imports, like fleet.py: the sensors in sensor.py
feed it and hold the timing.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# Shorter runs or smaller rises are mostly sensor steps (0.1-0.5 C) and lag.
MIN_RUN = timedelta(minutes=10)
MIN_RISE_C = 1.0
# Weight of each new run. A third keeps one odd run (a shower mid-heat) from
# moving the estimate far, while a real change (a new element) shows within a
# few days.
ALPHA = 0.3
# Outside this a reading is a sensor fault, not a tank: 0.5 C/h is a tank
# losing to a draw the whole time, 60 C/h a sensor strapped to the element.
MIN_RATE = 0.5
MAX_RATE = 60.0


class HeatRateLearner:
    """Moving average of C per hour over this tank's heating runs."""

    def __init__(self, rate: float | None = None, runs: int = 0) -> None:
        self.rate = rate
        self.runs = runs
        self._drawing = False
        self._start: tuple[datetime, float] | None = None
        self._last: tuple[datetime, float] | None = None

    @property
    def drawing(self) -> bool:
        return self._drawing

    def element(self, drawing: bool, now: datetime, temperature: float | None) -> bool:
        """Note the element starting or stopping. Returns True if it learned."""
        if drawing == self._drawing:
            return False
        self._drawing = drawing
        if drawing:
            self._start = self._last = (
                (now, temperature) if temperature is not None else None
            )
            return False
        learned = self._finish()
        self._start = self._last = None
        return learned

    def reading(self, now: datetime, temperature: float) -> None:
        """Note a tank reading. Only readings while drawing count."""
        if not self._drawing:
            return
        if self._start is None:
            self._start = (now, temperature)
        self._last = (now, temperature)

    def _finish(self) -> bool:
        if self._start is None or self._last is None:
            return False
        elapsed = self._last[0] - self._start[0]
        rise = self._last[1] - self._start[1]
        if elapsed < MIN_RUN or rise < MIN_RISE_C:
            return False
        rate = rise / (elapsed.total_seconds() / 3600)
        if not MIN_RATE <= rate <= MAX_RATE:
            return False
        self.rate = rate if self.rate is None else ALPHA * rate + (1 - ALPHA) * self.rate
        self.runs += 1
        return True


def ready_at(
    now: datetime,
    current: float | None,
    goal: float | None,
    rate: float | None,
    heating: bool,
) -> datetime | None:
    """When the tank should reach ``goal`` at the learned rate, if it is heating.

    None when it is not heating toward anything, is already there, or the rate
    is not learned yet: an estimate with no basis reads as a promise.
    """
    if not heating or current is None or goal is None or not rate:
        return None
    if current >= goal:
        return None
    return now + timedelta(hours=(goal - current) / rate)
