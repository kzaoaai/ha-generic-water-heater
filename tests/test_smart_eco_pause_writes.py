"""A timed Smart Eco pause writes its state when it starts and when it ends.

Its label used to be a countdown ("Resuming in 05H 59M") refreshed every
minute, so each paused heater rewrote both the water heater's state and the
Smart Eco State sensor's: two recorder rows a minute, for as long as the pause
lasted (up to 48 hours). When the pause ends is smart_eco_resume_at, an absolute
time a frontend can count down to by itself, so nothing needs rewriting while
the pause runs.

What these count: every state_changed and state_reported event for the heater
and its Smart Eco State sensor, in one-minute steps of frozen time. A change is
a new recorder row; a report is counted as well, so a tick that rewrote an
unchanged label cannot pass as silent. What they do not count: writes to other
entities, and anything a real temperature sensor or switch would cause, since
the world here stays still on purpose.
"""

from datetime import timedelta

from freezegun import freeze_time
import pytest
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import callback
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater import (
    CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
)
from custom_components.generic_water_heater.water_heater import OWN_ECHO_WINDOW
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"
STATE_SENSOR = "sensor.upstairs_smart_eco_state"
ECO_SELECT = "select.upstairs_smart_eco_mode"
WATCHED = (UPSTAIRS, STATE_SENSOR)
PAUSE = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def count_writes(hass):
    """Return a list that collects every state write of the watched entities."""
    writes = []

    @callback
    def _watched(event_data) -> bool:
        return event_data["entity_id"] in WATCHED

    @callback
    def _record(event) -> None:
        writes.append((event.event_type, event.data["entity_id"]))

    for event_type in (EVENT_STATE_CHANGED, EVENT_STATE_REPORTED):
        hass.bus.async_listen(event_type, _record, event_filter=_watched)
    return writes


async def step(hass, frozen, delta):
    """Advance frozen time and let every timer that came due run."""
    frozen.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def walk(hass, frozen, until):
    """Advance in one-minute steps, as the old countdown ticked, to `until`."""
    elapsed = timedelta(0)
    while elapsed + MINUTE <= until:
        await step(hass, frozen, MINUTE)
        elapsed += MINUTE


async def pause_by_turning_off(hass, frozen):
    """Auto Resume after Delay: turning the tank off is the timed pause."""
    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    return "Paused by manual control"


async def pause_by_the_wall_switch(hass, frozen):
    """Always ON: a person at the switch is a timed override of the policy."""
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": ECO_SELECT, "option": "Always ON"}, blocking=True,
    )
    await hass.async_block_till_done()
    # Past our own echo window, so the flip reads as a person.
    await step(hass, frozen, OWN_ECHO_WINDOW + timedelta(seconds=1))
    current = hass.states.get(UPSTAIRS_SWITCH).state
    hass.states.async_set(
        UPSTAIRS_SWITCH, STATE_OFF if current == STATE_ON else STATE_ON
    )
    await hass.async_block_till_done()
    return "Always ON override (Paused by manual control)"


async def paused(hass, frozen, start_pause):
    """Load both entries with eco permitting, pause upstairs, return the label."""
    entries = await setup_both(
        hass, **{CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS: int(PAUSE.total_seconds() // 3600)}
    )
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    label = await start_pause(hass, frozen)

    attrs = hass.states.get(UPSTAIRS).attributes
    assert attrs["smart_eco_pause_reason"] == "manual_off_timer"
    assert attrs["smart_eco_resume_at"] is not None
    return entries, label


@pytest.mark.parametrize(
    "start_pause", [pause_by_turning_off, pause_by_the_wall_switch]
)
async def test_a_timed_pause_writes_nothing_until_it_ends(hass, world, start_pause):  # noqa: F811
    with freeze_time(TRIGGER) as frozen:
        _, label = await paused(hass, frozen, start_pause)
        resume_at = hass.states.get(UPSTAIRS).attributes["smart_eco_resume_at"]
        assert hass.states.get(UPSTAIRS).attributes["smart_eco_state"] == label
        assert hass.states.get(STATE_SENSOR).state == label

        writes = count_writes(hass)
        await walk(hass, frozen, PAUSE - MINUTE)

        assert writes == [], (
            f"{len(writes)} state writes in {PAUSE - MINUTE} of a paused heater "
            f"that nothing touched"
        )
        attrs = hass.states.get(UPSTAIRS).attributes
        assert attrs["smart_eco_pause_reason"] == "manual_off_timer", (
            "the pause ended early, so the silence above proves nothing"
        )
        assert attrs["smart_eco_resume_at"] == resume_at
        assert attrs["smart_eco_state"] == label
        assert hass.states.get(STATE_SENSOR).state == label

        # The end of the pause is still published, so the counter is live.
        await walk(hass, frozen, 2 * MINUTE)
        assert hass.states.get(UPSTAIRS).attributes["smart_eco_pause_reason"] is None
        assert (EVENT_STATE_CHANGED, UPSTAIRS) in writes
        assert (EVENT_STATE_CHANGED, STATE_SENSOR) in writes


async def test_a_pause_restored_part_way_through_stays_silent(hass, world):  # noqa: F811
    """A reload re-arms the pause through its restore branch, which is separate.

    That branch used to start the countdown tick too, so a heater reloaded or
    restarted mid-pause would resume writing every minute even if the pause
    itself had not.
    """
    with freeze_time(TRIGGER) as frozen:
        (upstairs, _), label = await paused(hass, frozen, pause_by_turning_off)
        resume_at = hass.states.get(UPSTAIRS).attributes["smart_eco_resume_at"]
        await walk(hass, frozen, 10 * MINUTE)

        assert await hass.config_entries.async_reload(upstairs.entry_id)
        await hass.async_block_till_done()
        attrs = hass.states.get(UPSTAIRS).attributes
        assert attrs["smart_eco_pause_reason"] == "manual_off_timer"
        assert attrs["smart_eco_resume_at"] == resume_at, "the reload moved the deadline"
        assert hass.states.get(STATE_SENSOR).state == label

        writes = count_writes(hass)
        await walk(hass, frozen, PAUSE - 11 * MINUTE)
        assert writes == [], f"{len(writes)} state writes after the reload"
        assert hass.states.get(UPSTAIRS).attributes["smart_eco_pause_reason"] == (
            "manual_off_timer"
        )

        # The restored resume timer still ends it on the original deadline.
        await walk(hass, frozen, 2 * MINUTE)
        assert hass.states.get(UPSTAIRS).attributes["smart_eco_pause_reason"] is None
        assert (EVENT_STATE_CHANGED, STATE_SENSOR) in writes
