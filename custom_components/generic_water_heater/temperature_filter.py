"""Reject single-sample temperature spikes before anything acts on them.

Why this module exists
----------------------
On 2026-10-01 a tank sat at 61.9 C, 13.5 minutes into a disinfection hold. Its
heater's mode was changed, the relay switched, and 1.7 s later the temperature
sensor -- the same device as the relay -- reported 32.9 C once, then 62.0 C a
second later. The legionella sensor treats anything below its hard floor as a
real cold draw and discarded the hold on that one reading.

No body of water does that. 200 L cannot shed 29 C in a second and take it back
the next. But every consumer of the sensor believed it: the control loop saw a
cold tank, the draw detector saw a plunge, and max-temp history kept the point.
So the filter sits in front of all of them rather than inside one.

What it does
------------
A reading that moves more than ``SPIKE_JUMP_C`` from the last accepted one is
held back as *pending*, not delivered. The next reading decides it:

* back within ``SPIKE_JUMP_C`` of the last accepted value -> the pending
  reading was a glitch and is dropped; the new one is delivered.
* anywhere else -> the move was real; the pending reading is delivered with
  its own timestamp, then the new one is judged against it.

If nothing arrives within ``SPIKE_CONFIRM`` the pending reading is accepted
anyway. That bound is the caller's job (a timer) and it matters: sensors here
report on change, so a real step followed by a flat tank could otherwise sit
pending indefinitely, with the control loop acting on a stale value.

What it deliberately does not do
--------------------------------
* It never delays ``unavailable`` / ``unknown``. The control loop's failsafe
  turns the element off on those, and a filter must not stand between a dead
  sensor and that. They also clear the reference: after a gap there is nothing
  trustworthy to judge a jump against.
* It is not a smoother. Real draws and heating move in small steps between
  reports (the draw detector fires at under 1 C/min), far below the threshold,
  and pass through untouched. Only a single report-to-report step larger than
  any real tank produces is questioned, and even that costs at most one sample
  or ``SPIKE_CONFIRM``, never a lost reading.

Like ``fleet.py``, this imports nothing from Home Assistant and takes its
timestamps from the caller, so it can be unit tested without an event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# A single report-to-report step this large is not water. Heating moves a tank
# a fraction of a degree a minute and a draw a degree or two; a sensor reporting
# on 0.1-0.5 C of change never steps 10 C between two real readings.
SPIKE_JUMP_C = 10.0

# How long a questioned reading waits for a second opinion before it is taken
# at face value. Long enough for a chatty sensor to report again, short enough
# that a real step costs the control loop at most a minute.
SPIKE_CONFIRM = timedelta(seconds=60)


@dataclass(frozen=True)
class Reading:
    """A numeric temperature reading accepted for delivery."""

    when: datetime
    value: float


class SpikeFilter:
    """One-sample spike rejection for a single temperature source."""

    def __init__(self, jump_c: float = SPIKE_JUMP_C) -> None:
        self._jump_c = jump_c
        self._accepted: float | None = None
        self._pending: Reading | None = None

    @property
    def pending(self) -> Reading | None:
        """The reading currently held back awaiting confirmation, if any."""
        return self._pending

    def prime(self, value: float) -> None:
        """Take ``value`` as the reference without delivering it.

        Consumers seed themselves from the source's current state at startup,
        outside the filter. Priming with the same value means the first live
        reading is judged against it rather than accepted unconditionally.
        """
        self._accepted = value
        self._pending = None

    def reset(self) -> None:
        """Forget the reference, e.g. after the source went unavailable."""
        self._accepted = None
        self._pending = None

    def add(self, when: datetime, value: float) -> list[Reading]:
        """Judge one reading; return what should be delivered, in order."""
        reading = Reading(when, value)

        if self._pending is not None:
            pending, self._pending = self._pending, None
            if self._close(value, self._accepted):
                # The excursion did not persist: it was the sensor, not the tank.
                self._accepted = value
                return [reading]
            # It persisted. Deliver it, then judge the new reading against it.
            self._accepted = pending.value
            return [pending, *self.add(when, value)]

        if self._accepted is None or self._close(value, self._accepted):
            self._accepted = value
            return [reading]

        self._pending = reading
        return []

    def expire(self) -> list[Reading]:
        """Accept the pending reading because no second opinion arrived."""
        if self._pending is None:
            return []
        pending, self._pending = self._pending, None
        self._accepted = pending.value
        return [pending]

    def _close(self, value: float, reference: float | None) -> bool:
        return reference is not None and abs(value - reference) <= self._jump_c
