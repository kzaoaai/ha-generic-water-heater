"""The disinfection policy: a goal-seeking cycle that obeys everything above it.

The policy asks the tank to run PERFORMANCE until the risk sensor reports Low,
then hands it back. It deliberately does NOT outrank Smart Eco or a load shed --
both are checked ahead of the operation mode, so obeying them takes no code at
all, and that is the property most worth pinning down here.
"""

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.generic_water_heater import water_heater as water_heater_module
from custom_components.generic_water_heater import (
    DOMAIN,
    LEGIONELLA_MODE_OFF,
    LEGIONELLA_MODE_ON,
    LEGIONELLA_MODE_UNTIL_DISINFECTED,
    SERVICE_SHED,
    legionella_risk_signal,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"
SELECT = "select.upstairs_legionella_disinfection"


async def setup_with_policy(hass):
    """Load both entries with the risk sensor, and so the policy, enabled."""
    upstairs, downstairs = await setup_both(hass, enable_legionella_sensor=True)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
    return upstairs, downstairs


async def report_risk(hass, entry, risk):
    """Publish a risk verdict the way the sensor platform does."""
    hass.data[DOMAIN][entry.entry_id]["legionella_risk"] = risk
    async_dispatcher_send(hass, legionella_risk_signal(entry.entry_id), risk)
    await hass.async_block_till_done()


async def choose(hass, option):
    """Pick a policy through the select entity, as a person would."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": SELECT, "option": option},
        blocking=True,
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# The entity only exists where it was asked for
# ---------------------------------------------------------------------------


async def test_the_policy_select_is_gated_on_the_risk_sensor(hass, world):  # noqa: F811
    """Installs that never opted in must not grow a new entity."""
    await setup_both(hass)
    assert hass.states.get(SELECT) is None

    
async def test_the_policy_select_appears_when_the_sensor_is_enabled(hass, world):  # noqa: F811
    """With something to aim at, the policy is offered."""
    await setup_with_policy(hass)
    state = hass.states.get(SELECT)
    assert state is not None
    assert state.state == "Off"
    assert state.attributes["options"] == ["Off", "Until disinfected", "On"]


# ---------------------------------------------------------------------------
# Starting and finishing
# ---------------------------------------------------------------------------


async def test_off_does_nothing_however_bad_the_risk_gets(hass, world):  # noqa: F811
    """The default policy is inert."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "High")

    assert hass.states.get(UPSTAIRS).state == STATE_ELECTRIC
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False


async def test_a_requested_cycle_promotes_to_performance(hass, world):  # noqa: F811
    """The whole point: ask for it, and the tank chases 60 C."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    state = hass.states.get(UPSTAIRS)
    assert state.state == STATE_PERFORMANCE
    assert state.attributes["disinfection_active"] is True
    assert state.attributes["disinfection_return_mode"] == STATE_ELECTRIC


async def test_the_cycle_survives_the_nightly_eco_gap(hass, world):  # noqa: F811
    """Smart Eco parks the mode at OFF overnight and must bring it back.

    The eco gate restores smart_eco_last_heating_mode, so a cycle that did not
    also set that value would silently degrade to ordinary Electric on the first
    evening and never reach temperature again.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    assert (
        hass.states.get(UPSTAIRS).attributes["smart_eco_last_heating_mode"]
        == STATE_PERFORMANCE
    )

    hass.states.async_set(PV_EXCESS, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).state == STATE_OFF

    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE, (
        "the cycle degraded to electric across an eco gap"
    )


async def test_reaching_low_hands_the_tank_back(hass, world):  # noqa: F811
    """Finishing returns the tank to normal operation, not to performance."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    await report_risk(hass, upstairs, "Low")

    state = hass.states.get(UPSTAIRS)
    assert state.state == STATE_ELECTRIC
    assert state.attributes["disinfection_active"] is False
    assert state.attributes["smart_eco_last_heating_mode"] == STATE_ELECTRIC


async def test_until_disinfected_clears_itself(hass, world):  # noqa: F811
    """A one-shot must not re-arm; nothing starts again without a person."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")
    await report_risk(hass, upstairs, "Low")

    assert hass.states.get(SELECT).state == "Off"
    assert (
        hass.states.get(UPSTAIRS).attributes["legionella_mode"] == LEGIONELLA_MODE_OFF
    )

    # The interval lapses again -- and nothing happens.
    await report_risk(hass, upstairs, "Elevated")
    assert hass.states.get(UPSTAIRS).state == STATE_ELECTRIC


async def test_on_is_a_standing_policy_and_runs_again(hass, world):  # noqa: F811
    """The other half of the dropdown: keep doing this."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "On")
    await report_risk(hass, upstairs, "Low")

    assert hass.states.get(SELECT).state == "On"
    assert hass.states.get(UPSTAIRS).state == STATE_ELECTRIC

    await report_risk(hass, upstairs, "Elevated")
    assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE, (
        "a standing policy did not run the next cycle"
    )


async def test_choosing_off_mid_cycle_stops_it(hass, world):  # noqa: F811
    """A person changing their mind wins immediately."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")
    assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE

    await choose(hass, "Off")

    assert hass.states.get(UPSTAIRS).state == STATE_ELECTRIC
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False


# ---------------------------------------------------------------------------
# What it must never override
# ---------------------------------------------------------------------------


async def test_a_tank_switched_off_by_a_person_stays_off(hass, world):  # noqa: F811
    """Deliberate OFF is intent, and a maintenance policy does not overrule it."""
    upstairs, _ = await setup_with_policy(hass)
    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_OFF},
        blocking=True,
    )
    await hass.async_block_till_done()

    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "On")

    assert hass.states.get(UPSTAIRS).state == STATE_OFF
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False


async def test_smart_eco_still_blocks_a_disinfecting_tank(hass, world):  # noqa: F811
    """The cycle asks for heat; the eco gate still decides whether it gets it."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    hass.states.async_set(PV_EXCESS, STATE_OFF)
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).state == STATE_OFF
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF, (
        "a disinfection cycle kept the element on through an eco block"
    )
    # The request itself is still standing.
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True


async def test_a_load_shed_still_drops_a_disinfecting_tank(hass, world):  # noqa: F811
    """Supply protection outranks a maintenance cycle, with no special casing."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    await hass.services.async_call(
        DOMAIN, SERVICE_SHED, {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
    assert hass.states.get(UPSTAIRS).attributes["load_shed"] is True
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True


async def test_the_return_mode_is_never_performance(hass, world):  # noqa: F811
    """Reverting into an unbounded performance run is the failure to avoid."""
    upstairs, _ = await setup_with_policy(hass)
    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_PERFORMANCE},
        blocking=True,
    )
    await hass.async_block_till_done()

    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")
    assert hass.states.get(UPSTAIRS).attributes["disinfection_return_mode"] == (
        STATE_ELECTRIC
    )

    await report_risk(hass, upstairs, "Low")
    assert hass.states.get(UPSTAIRS).state == STATE_ELECTRIC


async def test_a_cycle_that_gets_nowhere_gives_up_and_says_so(hass, world):  # noqa: F811
    """One of the real tanks cannot reach 60 C inside a PV window at all.

    Left goal-seeking it would chase that forever, spending the surplus every
    afternoon and banking nothing. The bound is on the calendar, not on run
    length -- a run-length cap would abort the unpowered coast that actually
    earns the credit.
    """
    with freeze_time(TRIGGER):
        upstairs, _ = await setup_with_policy(hass)
        await report_risk(hass, upstairs, "Elevated")
        await choose(hass, "On")
        assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE

    with freeze_time(TRIGGER + timedelta(days=4)):
        # Any ordinary sensor update re-runs control, which re-checks the bound.
        with patch.object(water_heater_module.persistent_notification, "async_create") as told:
            hass.states.async_set(UPSTAIRS_SENSOR, "44.0")
            await hass.async_block_till_done()

    state = hass.states.get(UPSTAIRS)
    assert state.attributes["disinfection_active"] is False
    assert state.state == STATE_ELECTRIC
    assert state.attributes["legionella_mode"] == LEGIONELLA_MODE_OFF, (
        "a hopeless cycle re-armed itself instead of stopping"
    )
    assert hass.states.get(SELECT).state == "Off"

    assert told.called, "the owner was never told the cycle gave up"
    message = told.call_args.args[1]
    # Nothing was blocking this tank -- eco was permitting throughout -- so the
    # message must not blame Smart Eco for it.
    assert "cannot reach 60" in message
    assert "pause Smart Eco" not in message, (
        "the notification blamed Smart Eco when Smart Eco was permitting"
    )


async def test_the_give_up_notice_names_smart_eco_when_it_is_the_blocker(hass, world):  # noqa: F811
    """Telling the owner the wrong cause is worse than telling them nothing."""
    with freeze_time(TRIGGER):
        upstairs, _ = await setup_with_policy(hass)
        await report_risk(hass, upstairs, "Elevated")
        await choose(hass, "On")
        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()

    with freeze_time(TRIGGER + timedelta(days=4)):
        with patch.object(water_heater_module.persistent_notification, "async_create") as told:
            hass.states.async_set(UPSTAIRS_SENSOR, "44.0")
            await hass.async_block_till_done()

    assert told.called
    assert "pause Smart Eco" in told.call_args.args[1]


async def test_a_cycle_still_running_is_not_given_up_early(hass, world):  # noqa: F811
    """The bound must not fire during a legitimate multi-day chase."""
    with freeze_time(TRIGGER):
        upstairs, _ = await setup_with_policy(hass)
        await report_risk(hass, upstairs, "Elevated")
        await choose(hass, "On")

    with freeze_time(TRIGGER + timedelta(days=2)):
        hass.states.async_set(UPSTAIRS_SENSOR, "44.0")
        await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True


# ---------------------------------------------------------------------------
# A person taking the tank back mid-cycle
# ---------------------------------------------------------------------------


async def test_a_manual_mode_change_ends_the_cycle(hass, world):  # noqa: F811
    """Otherwise the flag is orphaned: set but with nothing left chasing it."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True

    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_ELECTRIC},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False


async def test_a_manual_mode_change_does_not_leave_performance_armed(hass, world):  # noqa: F811
    """The worst failure this feature could cause, so pin it hard.

    A cycle writes PERFORMANCE into smart_eco_last_heating_mode so the eco gate
    carries it overnight. If a person takes the tank back and that value is left
    behind, the eco gate faithfully restores PERFORMANCE the next time the sun
    comes out -- an unbounded run to the mechanical cutout that nobody asked for.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    # Turning the tank OFF is the path that reproduces it: set_operation_mode
    # only rewrites smart_eco_last_heating_mode for the two heating modes, so an
    # OFF leaves whatever the cycle put there behind.
    await hass.services.async_call(
        "water_heater",
        "turn_off",
        {"entity_id": UPSTAIRS},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert (
        hass.states.get(UPSTAIRS).attributes["smart_eco_last_heating_mode"]
        == STATE_ELECTRIC
    )

    # Sun goes down, comes back up. A manual OFF pauses Smart Eco, so lift that
    # first -- the question is what the eco gate restores, not whether it is
    # currently paused.
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.upstairs_smart_eco_mode", "option": "Auto Resume after Delay"},
        blocking=True,
    )
    await hass.async_block_till_done()
    hass.states.async_set(PV_EXCESS, STATE_OFF)
    await hass.async_block_till_done()
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).state != STATE_PERFORMANCE, (
        "an abandoned cycle resurrected itself as performance across an eco gap"
    )


async def test_taking_the_tank_back_stands_the_policy_down(hass, world):  # noqa: F811
    """A standing policy must not immediately yank it into performance again."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "On")

    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_ELECTRIC},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == "Off"

    await report_risk(hass, upstairs, "Elevated")
    assert hass.states.get(UPSTAIRS).state == STATE_ELECTRIC, (
        "the policy overrode a person who had just taken the tank back"
    )


async def test_asking_for_performance_mid_cycle_does_not_end_it(hass, world):  # noqa: F811
    """Asking for what the cycle already wants is not taking it back."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_PERFORMANCE},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True


async def test_a_cycle_is_not_stranded_when_smart_eco_is_switched_off(hass, world):  # noqa: F811
    """Smart Eco parks the mode at OFF; only its own restore branch lifts that.

    Switch Smart Eco off at that moment and the branch never runs again, so the
    tank sat at OFF with a cycle still nominally chasing it.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")

    hass.states.async_set(PV_EXCESS, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).state == STATE_OFF

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.upstairs_smart_eco_mode", "option": "Off"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE, (
        "the cycle was left stranded at OFF with nothing able to lift it"
    )


async def test_a_cycle_survives_a_reload(hass, world):  # noqa: F811
    """Both the water heater and the select persist the policy separately.

    They restore independently too, so the thing worth pinning is that they
    cannot come back disagreeing, and that a cycle in flight is still in flight.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "On")
    assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE

    await hass.config_entries.async_reload(upstairs.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(UPSTAIRS)
    assert state.attributes["disinfection_active"] is True, "a cycle was lost on reload"
    assert state.attributes["legionella_mode"] == LEGIONELLA_MODE_ON
    assert hass.states.get(SELECT).state == "On", (
        "the select and the water heater disagree about the policy after a reload"
    )


async def test_a_finished_policy_does_not_come_back_after_a_reload(hass, world):  # noqa: F811
    """A one-shot that cleared itself must stay cleared."""
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await choose(hass, "Until disinfected")
    await report_risk(hass, upstairs, "Low")
    assert hass.states.get(SELECT).state == "Off"

    await hass.config_entries.async_reload(upstairs.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == "Off"
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False


# ---------------------------------------------------------------------------
# Arming the policy is itself a request for heat
# ---------------------------------------------------------------------------


async def eco_off(hass):
    """Turn Smart Eco off entirely, as a person would."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.upstairs_smart_eco_mode", "option": "Off"},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_arming_the_policy_starts_a_cycle_on_a_tank_that_is_off(hass, world):  # noqa: F811
    """The documented way to force a cycle through, and it did nothing.

    With Smart Eco off there is no eco gate, so a tank sitting at OFF is
    indistinguishable from one a person switched off -- and the off-by-request
    guard swallowed the request. But choosing the policy IS the person asking,
    so it has to outrank a mode nobody has touched since.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await eco_off(hass)
    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(UPSTAIRS).state == STATE_OFF

    await choose(hass, "Until disinfected")

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True, (
        "arming the policy on an off tank did nothing"
    )
    assert hass.states.get(UPSTAIRS).state == STATE_PERFORMANCE


async def test_a_cycle_started_from_off_returns_the_tank_to_off(hass, world):  # noqa: F811
    """Reverting an off tank to ELECTRIC would leave it heating on grid.

    With Smart Eco off there is nothing to stop that, so the tank would quietly
    hold its target for ever on a cycle the person thought was one-shot.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Elevated")
    await eco_off(hass)
    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    await choose(hass, "Until disinfected")
    assert hass.states.get(UPSTAIRS).attributes["disinfection_return_mode"] == STATE_OFF

    await report_risk(hass, upstairs, "Low")

    assert hass.states.get(UPSTAIRS).state == STATE_OFF, (
        "a cycle started from off left the tank heating afterwards"
    )


async def test_a_standing_policy_still_leaves_an_off_tank_alone(hass, world):  # noqa: F811
    """The protection that guard existed for must survive the fix.

    Arming is a request. The interval lapsing weeks later is not -- by then the
    tank may be off because the house is empty.
    """
    upstairs, _ = await setup_with_policy(hass)
    await report_risk(hass, upstairs, "Low")
    await choose(hass, "On")
    await eco_off(hass)
    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()

    await report_risk(hass, upstairs, "Elevated")

    assert hass.states.get(UPSTAIRS).state == STATE_OFF, (
        "a standing policy resurrected a tank a person had switched off"
    )
    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False
