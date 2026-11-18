"""A Home Assistant restart must not end a surplus boost it cannot judge.

The owner's rule: a restart must not operate a breaker without a good reason.
The startup grace already keeps a false eco condition from parking a tank,
because at a start the condition cannot be evaluated yet. The boost had no such
allowance. A surplus entity that is missing, unavailable or still inside its
own delay reads inactive, so a tank being boosted above its normal target was
"satisfied" at load: the setup control pass switched the element off, and the
boost switched it on again when the entity came back, about a minute later
(a reviewer's repro, 2026-11-18; on the real install the surplus sensor was on
before the 2026-11-15 15:10 restart and read off for 60 s after it).

What the boost's inputs said when Home Assistant went down is carried through
the startup grace: surplus until it is seen active again, the forecast until
it can be read again. Nothing is carried past the grace, on a reload, or from a
restart that happened while a carry was itself in force.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, State
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.generic_water_heater import (
    CONF_ECO_TEMPLATE,
    CONF_SURPLUS_BOOST_TARGET,
    CONF_SURPLUS_ENTITY,
    CONF_SURPLUS_STATE,
    CONF_UTILITY_ENTITY,
    CONF_UTILITY_STATE,
    SMART_ECO_MODE_AUTO_RESUME,
)
from custom_components.generic_water_heater.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.generic_water_heater.water_heater import (
    STARTUP_GRACE,
    SURPLUS_BOOST_SHORT_RUN,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    commanded,
    entities,
    world,
)

UPSTAIRS = "water_heater.upstairs"
FORECAST = "sensor.energy_production_tomorrow"
SOURCE = "sensor.home_power_source"
START = datetime(2026, 11, 15, 13, 10, 50, tzinfo=timezone.utc)
ABSENT = None

# What a heater publishes while it boosts on a surplus it can see.
BOOSTING = {
    "temperature": 50,
    "smart_eco_mode": SMART_ECO_MODE_AUTO_RESUME,
    "smart_eco_last_heating_mode": "electric",
    "surplus_boost_enabled": True,
    "surplus_boost_active": True,
    "surplus_active": True,
}


def heater(hass):
    return hass.states.get(UPSTAIRS)


def relay(hass):
    return hass.states.get(UPSTAIRS_SWITCH).state


def say(hass, entity_id, word, **attributes):
    if word is ABSENT:
        hass.states.async_remove(entity_id)
    else:
        hass.states.async_set(entity_id, word, attributes)


async def restarted(
    hass,
    *,
    restored=BOOSTING,
    surplus=ABSENT,
    tank="55",
    relay_found=STATE_ON,
    starting=True,
    mode="electric",
    **extra,
):
    """Load one heater the way a start does. Normal target 50, boost target 61.

    At 55 the tank is satisfied unless it is boosting, so a boost that is lost
    is an element switched off.
    """
    mock_restore_cache(hass, (State(UPSTAIRS, mode, dict(restored)),))
    say(hass, PV_EXCESS, surplus)
    hass.states.async_set(UPSTAIRS_SENSOR, tank, {"device_class": "temperature"})
    say(hass, UPSTAIRS_SWITCH, relay_found)
    if starting:
        hass.set_state(CoreState.starting)
    entry = build_entry(
        "Upstairs",
        UPSTAIRS_SWITCH,
        UPSTAIRS_SENSOR,
        stagger_seconds=0.0,
        target_temp=50.0,
        **{
            CONF_ECO_TEMPLATE: "",
            CONF_SURPLUS_ENTITY: PV_EXCESS,
            CONF_SURPLUS_STATE: "on",
            CONF_SURPLUS_BOOST_TARGET: 61.0,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def finish_starting(hass):
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


async def tick(hass, frozen, delta):
    frozen.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


# (the surplus entity when the heater loads, what it says next, in order).
# The second is the real install: its sensor reads a plausible "off" for a
# minute after a start, in full sun.
COMING_BACK = [
    pytest.param(ABSENT, [STATE_ON], id="absent-on"),
    pytest.param(ABSENT, [STATE_OFF, STATE_ON], id="absent-off-on"),
    pytest.param(STATE_OFF, [STATE_ON], id="off-on"),
    pytest.param(STATE_UNAVAILABLE, [STATE_OFF, STATE_ON], id="unavailable-off-on"),
    pytest.param(STATE_UNKNOWN, [STATE_UNAVAILABLE, STATE_ON], id="unknown-unavailable-on"),
]


@pytest.mark.parametrize(("at_load", "words"), COMING_BACK)
async def test_a_restart_in_the_middle_of_a_boost_does_not_cycle_the_relay(
    hass, world, at_load, words  # noqa: F811
):
    with freeze_time(START) as frozen:
        await restarted(hass, surplus=at_load)
        assert relay(hass) == STATE_ON, "switched off at load"
        assert heater(hass).attributes["surplus_boost_active"] is True

        await finish_starting(hass)
        for word in words:
            await tick(hass, frozen, timedelta(seconds=30))
            say(hass, PV_EXCESS, word)
            await hass.async_block_till_done()
            assert relay(hass) == STATE_ON, f"switched off when the surplus said {word!r}"

        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"]), "the relay was cycled"
        assert heater(hass).attributes["surplus_boost_active"] is True
        assert heater(hass).attributes["surplus_active"] is True

        # The window ends and nothing changes: the surplus is live again.
        await tick(hass, frozen, STARTUP_GRACE)
        assert relay(hass) == STATE_ON
        assert heater(hass).attributes["surplus_boost_active"] is True

        # From here the boost follows the surplus as it always did.
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["surplus_boost_active"] is False


@pytest.mark.parametrize("at_load", [ABSENT, STATE_OFF, STATE_UNAVAILABLE])
async def test_a_carried_boost_ends_with_the_startup_window(hass, world, at_load):  # noqa: F811
    """A surplus that never comes back must not be boosted on for ever."""
    with freeze_time(START) as frozen:
        await restarted(hass, surplus=at_load)
        await finish_starting(hass)

        await tick(hass, frozen, STARTUP_GRACE - timedelta(seconds=5))
        assert relay(hass) == STATE_ON, "ended before the window did"
        assert heater(hass).attributes["surplus_boost_active"] is True

        await tick(hass, frozen, timedelta(seconds=10))
        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["surplus_boost_active"] is False

        # And it is not carried again later, by anything.
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        await tick(hass, frozen, STARTUP_GRACE)
        assert relay(hass) == STATE_OFF


async def test_a_carried_boost_ends_with_the_window_while_eco_still_allows_heat(
    hass, world  # noqa: F811
):
    """The end of the carry is the boost's own, not the eco gate parking the tank.

    The utility carries the house and may heat the tank, to its normal target.
    """
    with freeze_time(START) as frozen:
        hass.states.async_set(SOURCE, "Utility")
        await restarted(
            hass,
            restored={**BOOSTING, "eco_use_utility": True},
            **{CONF_UTILITY_ENTITY: SOURCE, CONF_UTILITY_STATE: "Utility"},
        )
        assert relay(hass) == STATE_ON
        await finish_starting(hass)

        await tick(hass, frozen, STARTUP_GRACE + timedelta(seconds=5))
        assert heater(hass).state == "electric", "test premise: not parked"
        assert heater(hass).attributes["surplus_boost_active"] is False
        assert relay(hass) == STATE_OFF


async def test_the_window_is_counted_from_the_end_of_the_start(hass, world):  # noqa: F811
    """A slow start is not the surplus's absence, exactly as for the eco gate."""
    with freeze_time(START) as frozen:
        await restarted(hass)
        await tick(hass, frozen, STARTUP_GRACE * 3)
        assert relay(hass) == STATE_ON
        assert heater(hass).attributes["surplus_boost_active"] is True

        await finish_starting(hass)
        await tick(hass, frozen, STARTUP_GRACE + timedelta(seconds=5))
        assert relay(hass) == STATE_OFF


async def test_a_carry_publishes_the_surplus_it_can_see(hass, world):  # noqa: F811
    """The boost is reported as running; the surplus is reported as read.

    surplus_active is what the next restart restores. Publishing the carried
    value there would let each restart inside the window carry the boost for
    another one, and a crash loop would boost on no surplus at all.
    """
    with freeze_time(START):
        entry = await restarted(hass, surplus=STATE_OFF)

        assert heater(hass).attributes["surplus_boost_active"] is True
        assert heater(hass).attributes["surplus_active"] is False
        # Nor is it the eco condition: that is still "cannot be judged yet".
        assert heater(hass).attributes["smart_eco_condition_met"] is False
        assert heater(hass).attributes["smart_eco_state"] == "Starting up"
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["boost_surplus_carried"] is True


async def test_a_carried_surplus_is_not_the_eco_condition(hass, world):  # noqa: F811
    """It keeps a boost that was running. It allows nothing that was not.

    A true eco condition is acted on at once, also inside the startup grace:
    it brings a parked tank back. A surplus nobody can see must not do that.
    """
    with freeze_time(START):
        await restarted(hass, mode=STATE_OFF, relay_found=STATE_OFF, tank="40")

        assert heater(hass).attributes["smart_eco_condition_met"] is False
        assert heater(hass).state == STATE_OFF, "a parked tank was brought back"
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])


async def test_a_carried_boost_is_not_reported_as_eco_gated(hass, world):  # noqa: F811
    """eco_gated tells a battery estimate that this draw stops when the surplus leaves.

    A carried boost does not: it cannot see the surplus. Reported as gated, it
    would hide a load the battery may be carrying for the length of the window.
    """
    with freeze_time(START):
        await restarted(hass, surplus=STATE_OFF)
        assert heater(hass).attributes["surplus_boost_active"] is True
        assert relay(hass) == STATE_ON
        assert heater(hass).attributes["eco_gated"] is False

        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert heater(hass).attributes["eco_gated"] is True


SAVED_BY_4_6_6 = {k: v for k, v in BOOSTING.items() if k != "surplus_active"}


async def test_the_restart_that_installs_this_version_is_covered(hass, world):  # noqa: F811
    """A state saved by 4.6.6 has no surplus_active, only the boost's own flag.

    There the boost was active only on a surplus that was read, so the flag
    says what the missing key would have.
    """
    with freeze_time(START) as frozen:
        await restarted(hass, restored=SAVED_BY_4_6_6, surplus=STATE_OFF)
        assert relay(hass) == STATE_ON, "switched off at load"

        await finish_starting(hass)
        await tick(hass, frozen, timedelta(seconds=60))
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"]), "the relay was cycled"


@pytest.mark.parametrize(
    "restored",
    [
        pytest.param({**BOOSTING, "surplus_active": False}, id="restarted-inside-a-carry"),
        pytest.param(
            {**SAVED_BY_4_6_6, "surplus_boost_active": False}, id="saved-by-4.6.6-not-boosting"
        ),
        pytest.param({**BOOSTING, "surplus_active": "on"}, id="not-a-boolean"),
        pytest.param({**BOOSTING, "surplus_active": None}, id="null"),
    ],
)
async def test_only_a_surplus_that_was_seen_is_carried(hass, world, restored):  # noqa: F811
    """No carry from a carry, and none from a state that never said.

    The first is the crash loop: a carry publishes surplus_active false and
    surplus_boost_active true, and the next start must believe the first.
    """
    with freeze_time(START):
        entry = await restarted(hass, restored=restored, surplus=STATE_OFF)

        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["surplus_boost_active"] is False
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["boost_surplus_carried"] is False


async def test_a_reload_carries_nothing(hass, world):  # noqa: F811
    """A reload happens with every source loaded: off means off."""
    with freeze_time(START):
        entry = await restarted(hass, surplus=STATE_OFF, starting=False)

        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["surplus_boost_active"] is False
        report = (await async_get_config_entry_diagnostics(hass, entry))["heater"]
        assert report["boost_surplus_carried"] is False


async def test_a_surplus_seen_again_is_live_from_then_on(hass, world):  # noqa: F811
    """Once the entity has said on, its off is a real one, window or no window.

    The run is longer than a short one on purpose: a short run that loses its
    surplus is locked out, and the lockout would end this boost whether or not
    the carry had been dropped.
    """
    with freeze_time(START) as frozen:
        await restarted(hass, surplus=STATE_ON)
        assert relay(hass) == STATE_ON

        # Still starting, so still inside the window.
        await tick(hass, frozen, SURPLUS_BOOST_SHORT_RUN + timedelta(minutes=1))
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()

        assert heater(hass).attributes["surplus_boost_locked_until"] is None, "test premise"
        assert heater(hass).attributes["smart_eco_state"] == "Starting up", "test premise"
        assert relay(hass) == STATE_OFF, "carried a surplus that had just left"
        assert heater(hass).attributes["surplus_boost_active"] is False


@pytest.mark.parametrize(
    "restored",
    [
        pytest.param({**BOOSTING, "surplus_boost_enabled": False}, id="boost-switched-off"),
        pytest.param(
            {
                **BOOSTING,
                "surplus_boost_locked_until": (START + timedelta(minutes=20)).isoformat(),
            },
            id="locked-out",
        ),
        pytest.param({**BOOSTING, "load_shed": True}, id="shed"),
    ],
)
async def test_a_carried_surplus_overrules_nothing_else(hass, world, restored):  # noqa: F811
    """Only the surplus reading is carried. What else stops a boost still does."""
    with freeze_time(START):
        await restarted(hass, restored=restored)

        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["surplus_boost_active"] is False


@pytest.mark.parametrize("surplus", [ABSENT, STATE_OFF, STATE_UNAVAILABLE])
async def test_a_carry_never_switches_an_element_on(hass, world, surplus):  # noqa: F811
    """It keeps a running boost running. It starts nothing.

    What was saved can be stale: a crash before the next save, a power cut,
    hours of downtime. A relay found off, with the tank between its two
    targets, was switched on by a surplus nobody had read (two reviewers'
    repros), which is a restart operating a breaker in the worse direction.
    """
    with freeze_time(START) as frozen:
        await restarted(hass, surplus=surplus, relay_found=STATE_OFF)
        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["surplus_boost_active"] is False

        await finish_starting(hass)
        await tick(hass, frozen, STARTUP_GRACE - timedelta(seconds=5))
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"]), "energised by a carry"

        # A surplus that is read starts the boost, as it always did.
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON


@pytest.mark.parametrize("relay_found", [STATE_UNAVAILABLE, STATE_UNKNOWN, ABSENT])
async def test_a_relay_that_cannot_be_read_is_not_one_that_is_on(
    hass, world, relay_found  # noqa: F811
):
    """The carry needs an element it can see running.

    A relay that is off the network, has not reported yet or does not exist
    when the heater loads may be on or off. Counting it as on would send it an
    ON on the strength of a carried reading, and whether that is dropped is
    the relay's integration's business, not something to rely on.
    """
    with freeze_time(START):
        await restarted(hass, relay_found=relay_found)

        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"])
        # Asked of the entity: with no switch the heater publishes unavailable,
        # and that carries no attributes to read.
        assert entities(hass)["Upstairs"]._surplus_boost_active() is False


async def test_a_carried_boost_that_stops_is_not_started_again(hass, world):  # noqa: F811
    """Reaching the boost target inside the window ends the carry's say.

    The element goes off at the target, as in any boost. When the tank cools
    again the surplus still has not been read, so nothing switches it back on.
    """
    with freeze_time(START) as frozen:
        await restarted(hass)
        assert relay(hass) == STATE_ON

        await tick(hass, frozen, timedelta(seconds=30))
        hass.states.async_set(UPSTAIRS_SENSOR, "61", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF, "test premise: the boost stops at its target"
        world["turn_on"].clear()

        await tick(hass, frozen, timedelta(seconds=30))
        hass.states.async_set(UPSTAIRS_SENSOR, "55", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"]), "energised by a carry"
        assert relay(hass) == STATE_OFF


async def test_a_paused_tank_found_off_is_not_started_by_a_carry(hass, world):  # noqa: F811
    """A person switched it off at the wall in the sun; then came a restart."""
    with freeze_time(START) as frozen:
        await restarted(
            hass,
            restored={
                **BOOSTING,
                "smart_eco_pause_reason": "manual_off_timer",
                "smart_eco_resume_at": (START + timedelta(hours=2)).isoformat(),
            },
            surplus=STATE_OFF,
            relay_found=STATE_OFF,
        )
        await finish_starting(hass)
        await tick(hass, frozen, STARTUP_GRACE + timedelta(seconds=5))

        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"]), "energised by a carry"
        assert relay(hass) == STATE_OFF


# --- forecast pre-heat -----------------------------------------------------------

PREHEAT = {
    "enable_forecast_preheat": True,
    "forecast_entity": FORECAST,
    "forecast_min_kwh": 20.0,
    "forecast_preheat_target_temperature": 70.0,
}
PREHEATING = {**BOOSTING, "forecast_preheat_active": True}


async def preheating_restarted(
    hass, *, forecast=ABSENT, restored=PREHEATING, surplus=STATE_ON, **kwargs
):
    """Boost target 61, pre-heat target 70, tank at 65: on only while pre-heating."""
    say(hass, FORECAST, forecast, unit_of_measurement="kWh")
    kwargs.setdefault("tank", "65")
    return await restarted(hass, restored=restored, surplus=surplus, **PREHEAT, **kwargs)


@pytest.mark.parametrize(
    ("at_load", "words"),
    [
        pytest.param(ABSENT, ["9"], id="absent-poor"),
        pytest.param(ABSENT, [STATE_UNAVAILABLE, "9"], id="absent-unavailable-poor"),
        pytest.param(STATE_UNAVAILABLE, [STATE_UNKNOWN, "9"], id="unavailable-unknown-poor"),
        pytest.param(STATE_UNKNOWN, ["nonsense", "9"], id="unknown-nonsense-poor"),
    ],
)
async def test_a_restart_in_the_middle_of_a_pre_heat_does_not_cycle_the_relay(
    hass, world, at_load, words  # noqa: F811
):
    """An unreadable forecast is "not poor", which ended the pre-heat at load."""
    with freeze_time(START) as frozen:
        await preheating_restarted(hass, forecast=at_load)
        assert relay(hass) == STATE_ON, "switched off at load"
        assert heater(hass).attributes["forecast_preheat_active"] is True

        await finish_starting(hass)
        for word in words:
            await tick(hass, frozen, timedelta(seconds=20))
            say(hass, FORECAST, word, unit_of_measurement="kWh")
            await hass.async_block_till_done()
            assert relay(hass) == STATE_ON, f"switched off when the forecast said {word!r}"

        assert relay(hass) == STATE_ON
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"]), "the relay was cycled"

        await tick(hass, frozen, STARTUP_GRACE)
        assert relay(hass) == STATE_ON


async def test_a_carried_forecast_ends_with_the_startup_window(hass, world):  # noqa: F811
    with freeze_time(START) as frozen:
        await preheating_restarted(hass)
        await finish_starting(hass)

        await tick(hass, frozen, STARTUP_GRACE - timedelta(seconds=5))
        assert relay(hass) == STATE_ON, "ended before the window did"

        await tick(hass, frozen, timedelta(seconds=10))
        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["forecast_preheat_active"] is False


@pytest.mark.parametrize(
    "restored",
    [
        pytest.param({**BOOSTING, "forecast_preheat_active": False}, id="was-not-poor"),
        pytest.param(dict(BOOSTING), id="never-said"),
    ],
)
async def test_only_a_forecast_that_was_poor_is_carried(hass, world, restored):  # noqa: F811
    with freeze_time(START):
        await preheating_restarted(hass, restored=restored)

        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["forecast_preheat_active"] is False


async def test_a_pre_heat_since_taken_out_of_the_options_is_not_carried(hass, world):  # noqa: F811
    """The saved state says pre-heating; this heater no longer has a forecast."""
    with freeze_time(START):
        await restarted(hass, restored=PREHEATING, surplus=STATE_ON, tank="65")

        assert heater(hass) is not None, "the heater did not load"
        assert relay(hass) == STATE_OFF


async def test_a_forecast_that_can_be_read_is_believed(hass, world):  # noqa: F811
    """Carried only while unreadable: a good forecast ends the pre-heat at once."""
    with freeze_time(START) as frozen:
        await preheating_restarted(hass)
        assert relay(hass) == STATE_ON

        await tick(hass, frozen, timedelta(seconds=20))
        say(hass, FORECAST, "40", unit_of_measurement="kWh")
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF

        # Having been read, it is not carried again if it drops out.
        say(hass, FORECAST, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF, "a forecast that had been read was carried again"
        assert heater(hass).attributes["forecast_preheat_active"] is False


@pytest.mark.parametrize("surplus_at_load", [STATE_ON, ABSENT])
async def test_a_carried_forecast_never_switches_an_element_on(
    hass, world, surplus_at_load  # noqa: F811
):
    """The forecast's carry obeys the surplus's rule: keep on, never switch on.

    A reviewer's repro. The relay is off, the surplus is read active (at load,
    or a minute into the window) and the forecast is still unreadable. The
    carried "poor" raised the target from 61 to 70 over a tank at 65 and the
    heater switched the element on, on a forecast saved before the restart.
    """
    with freeze_time(START) as frozen:
        await preheating_restarted(hass, relay_found=STATE_OFF, surplus=surplus_at_load)
        await finish_starting(hass)
        await tick(hass, frozen, timedelta(seconds=60))
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

        assert UPSTAIRS_SWITCH not in commanded(world["turn_on"]), "energised by a carry"
        assert relay(hass) == STATE_OFF
        assert heater(hass).attributes["forecast_preheat_active"] is False

        # A forecast that is read starts the pre-heat, as it always did.
        say(hass, FORECAST, "9", unit_of_measurement="kWh")
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON


async def test_a_forecast_read_poor_again_is_not_carried_when_it_drops_out(
    hass, world  # noqa: F811
):
    """Read again, it speaks for itself whatever it said: unreadable is not poor."""
    with freeze_time(START) as frozen:
        await preheating_restarted(hass)
        await tick(hass, frozen, timedelta(seconds=20))
        say(hass, FORECAST, "9", unit_of_measurement="kWh")
        await hass.async_block_till_done()
        assert relay(hass) == STATE_ON, "test premise: poor, pre-heating"

        say(hass, FORECAST, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF, "a forecast that had been read was carried again"
        assert heater(hass).attributes["forecast_preheat_active"] is False


async def test_a_forecast_readable_at_load_is_never_carried(hass, world):  # noqa: F811
    """Good when the heater loads, then gone: nothing was carried to fall back on.

    The element is on throughout, boosting to 61 on a surplus that is read, so
    a carried "poor" would be in force and would take it past 61 toward 70.
    """
    with freeze_time(START) as frozen:
        await preheating_restarted(hass, forecast="40", tank="58")
        assert relay(hass) == STATE_ON, "test premise: boosting toward 61"
        assert heater(hass).attributes["forecast_preheat_active"] is False

        await tick(hass, frozen, timedelta(seconds=20))
        say(hass, FORECAST, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert heater(hass).attributes["forecast_preheat_active"] is False

        hass.states.async_set(UPSTAIRS_SENSOR, "62", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert relay(hass) == STATE_OFF, "heated past the boost target on a carried forecast"


async def test_surplus_and_forecast_are_carried_together(hass, world):  # noqa: F811
    """The real restart: neither source has loaded when the heater does."""
    with freeze_time(START) as frozen:
        say(hass, FORECAST, ABSENT)
        await restarted(hass, restored=PREHEATING, tank="65", **PREHEAT)
        assert relay(hass) == STATE_ON

        await finish_starting(hass)
        await tick(hass, frozen, timedelta(seconds=30))
        say(hass, PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        await tick(hass, frozen, timedelta(seconds=30))
        say(hass, FORECAST, "9", unit_of_measurement="kWh")
        await hass.async_block_till_done()
        await tick(hass, frozen, timedelta(seconds=30))
        say(hass, PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

        assert relay(hass) == STATE_ON
        assert UPSTAIRS_SWITCH not in commanded(world["turn_off"]), "the relay was cycled"
