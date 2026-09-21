"""The transient Smart Eco mode, and "Disinfect ASAP" which rides on it.

"Off until target reached" stands Smart Eco down and gives it back by itself.
Two things make it more than a relabelled Off: it has to remember what to
revert TO, and it has to be bounded -- the whole reason for asking for it was
not wanting to leave eco off by accident.
"""

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.const import STATE_ON
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from unittest.mock import patch

from custom_components.generic_water_heater import (
    SMART_ECO_MODE_ALWAYS_ON,
    SMART_ECO_MODE_AUTO_RESUME,
    SMART_ECO_MODE_OFF,
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
)
from custom_components.generic_water_heater import water_heater as water_heater_module
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    TRIGGER,
    UPSTAIRS_SENSOR,
    auto_enable_custom_integrations,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"
ECO_SELECT = "select.upstairs_smart_eco_mode"


async def eco(hass, option):
    """Pick a Smart Eco mode as a person would."""
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": ECO_SELECT, "option": option}, blocking=True,
    )
    await hass.async_block_till_done()


def eco_mode(hass):
    return hass.states.get(UPSTAIRS).attributes["smart_eco_mode"]


async def blocked(hass):
    """Load both entries with Smart Eco enforcing and its condition FALSE."""
    await setup_both(hass)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, "off")
        await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# It is a bypass, and it remembers what to give back
# ---------------------------------------------------------------------------


async def test_it_stops_eco_blocking_the_tank(hass, world):  # noqa: F811
    """The point of the mode: heat now, whatever the eco condition says."""
    await blocked(hass)
    assert hass.states.get(UPSTAIRS).attributes["smart_eco_state"] == (
        "Blocked by eco condition"
    )

    assert hass.states.get(UPSTAIRS).state == "off", "eco should have parked it"

    await eco(hass, "Off until target reached")

    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET
    assert hass.states.get(UPSTAIRS).attributes["smart_eco_state"] == (
        "Off until the tank reaches target"
    )
    # The mode is worthless if the tank stays where eco parked it. Only its own
    # restore branch lifts that park, and this mode skips that branch.
    assert hass.states.get(UPSTAIRS).state != "off", (
        "the tank was left sitting off, which is the opposite of the request"
    )


async def test_it_remembers_the_mode_it_replaced(hass, world):  # noqa: F811
    """Reverting to a hardcoded default would silently change the policy."""
    await blocked(hass)
    await eco(hass, "Always ON")
    assert eco_mode(hass) == SMART_ECO_MODE_ALWAYS_ON

    await eco(hass, "Off until target reached")

    assert hass.states.get(UPSTAIRS).attributes["smart_eco_previous_mode"] == (
        SMART_ECO_MODE_ALWAYS_ON
    )


async def test_reselecting_it_does_not_overwrite_what_it_reverts_to(hass, world):  # noqa: F811
    """Otherwise the second selection makes it revert to itself, i.e. never.

    Note what actually protects this today: async_set_smart_eco_mode returns
    early when the mode is unchanged, so the second selection never reaches the
    bookkeeping. The explicit guard there is belt-and-braces against that early
    return being relaxed; this test pins the OBSERVABLE property rather than
    claiming to exercise the guard.
    """
    await blocked(hass)
    await eco(hass, "Off until target reached")
    await eco(hass, "Off until target reached")

    previous = hass.states.get(UPSTAIRS).attributes["smart_eco_previous_mode"]
    assert previous != SMART_ECO_MODE_OFF_UNTIL_TARGET
    assert previous == SMART_ECO_MODE_AUTO_RESUME


async def test_choosing_another_mode_clears_the_revert_target(hass, world):  # noqa: F811
    """A person overriding the bypass ends it; nothing should linger."""
    await blocked(hass)
    await eco(hass, "Off until target reached")
    await eco(hass, "Off")

    assert eco_mode(hass) == SMART_ECO_MODE_OFF
    assert hass.states.get(UPSTAIRS).attributes["smart_eco_previous_mode"] is None


# ---------------------------------------------------------------------------
# It must not be able to leave eco off for ever
# ---------------------------------------------------------------------------


async def test_it_gives_eco_back_when_the_tank_cannot_reach_target(hass, world):  # noqa: F811
    """A dead element or an unreachable target must not disable eco for ever.

    This is the failure the mode was asked for in order to AVOID, so it is the
    one behaviour that cannot be left to the happy path.
    """
    with freeze_time(TRIGGER):
        await blocked(hass)
        await eco(hass, "Off until target reached")
        assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET

    # The harness leaves smart_eco_manual_off_resume_hours at its 6 h default
    # (prod runs 3 h upstairs / 6 h downstairs), so step past that.
    with freeze_time(TRIGGER + timedelta(hours=7)):
        with patch.object(
            water_heater_module.persistent_notification, "async_create"
        ) as told:
            hass.states.async_set(UPSTAIRS_SENSOR, "30.0")
            await hass.async_block_till_done()

    assert eco_mode(hass) != SMART_ECO_MODE_OFF_UNTIL_TARGET, (
        "smart eco was left disabled past its bound"
    )
    assert told.called, "eco was restored without telling anyone why"


async def test_it_does_not_give_eco_back_early(hass, world):  # noqa: F811
    """The bound must not fire while the tank is still legitimately heating."""
    with freeze_time(TRIGGER):
        await blocked(hass)
        await eco(hass, "Off until target reached")

    with freeze_time(TRIGGER + timedelta(hours=1)):
        hass.states.async_set(UPSTAIRS_SENSOR, "30.0")
        await hass.async_block_till_done()

    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET


# ---------------------------------------------------------------------------
# Disinfect ASAP rides on the same mechanism
# ---------------------------------------------------------------------------


LEG_SELECT = "select.upstairs_legionella_disinfection"


async def setup_with_policy_blocked(hass):
    """Both entries with the risk sensor enabled and the eco condition FALSE."""
    entries = await setup_both(hass, enable_legionella_sensor=True)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, "off")
        await hass.async_block_till_done()
    return entries


async def report_risk(hass, entry, risk):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.generic_water_heater import DOMAIN, legionella_risk_signal

    hass.data[DOMAIN][entry.entry_id]["legionella_risk"] = risk
    async_dispatcher_send(hass, legionella_risk_signal(entry.entry_id), risk)
    await hass.async_block_till_done()


async def test_disinfect_asap_stands_eco_down_by_itself(hass, world):  # noqa: F811
    """The point of ASAP: do not wait for the sun.

    Expressed through the transient eco mode rather than a second bypass, so
    there is one thing that stands eco down and one bounded thing that gives it
    back.
    """
    upstairs, _ = await setup_with_policy_blocked(hass)
    await report_risk(hass, upstairs, "Elevated")

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect ASAP"}, blocking=True,
    )
    await hass.async_block_till_done()

    attrs = hass.states.get(UPSTAIRS).attributes
    assert attrs["disinfection_active"] is True
    assert attrs["smart_eco_mode"] == SMART_ECO_MODE_OFF_UNTIL_TARGET, (
        "ASAP did not stand Smart Eco down, so it would wait for PV after all"
    )
    assert hass.states.get(UPSTAIRS).state == "performance"


async def test_plain_disinfect_leaves_eco_alone(hass, world):  # noqa: F811
    """The difference between the two one-shots is exactly this."""
    upstairs, _ = await setup_with_policy_blocked(hass)
    await report_risk(hass, upstairs, "Elevated")

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["smart_eco_mode"] == (
        SMART_ECO_MODE_AUTO_RESUME
    )


async def test_eco_is_not_given_back_while_the_cycle_is_still_running(hass, world):  # noqa: F811
    """PERFORMANCE never reads idle, so "target reached" must not mean idle.

    If it did, a cycle would hand eco back the moment it started and then stall
    waiting for sun -- the failure this whole arrangement exists to avoid.
    """
    upstairs, _ = await setup_with_policy_blocked(hass)
    await report_risk(hass, upstairs, "Elevated")
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect ASAP"}, blocking=True,
    )
    await hass.async_block_till_done()

    hass.states.async_set(UPSTAIRS_SENSOR, "58.0")
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True
    assert hass.states.get(UPSTAIRS).attributes["smart_eco_mode"] == (
        SMART_ECO_MODE_OFF_UNTIL_TARGET
    )



async def test_a_load_shed_mid_cycle_does_not_hand_eco_back(hass, world):  # noqa: F811
    """A shed must not hand eco back while the cycle is still outstanding.

    Correction to an earlier note here: a shed does NOT drive hvac_action to
    off. It leaves the operation mode alone and forces only the switch, so it
    reads "idle" -- which is why the shed needed a guard of its own (see
    test_a_shed_is_not_mistaken_for_a_satisfied_tank). While a cycle is running
    the "not disinfecting" clause covers it too, so this test passes on either
    guard; it pins the outcome, not which clause delivers it.
    """
    from custom_components.generic_water_heater import DOMAIN, SERVICE_SHED

    upstairs, _ = await setup_with_policy_blocked(hass)
    await report_risk(hass, upstairs, "Elevated")
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect ASAP"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET

    await hass.services.async_call(
        DOMAIN, SERVICE_SHED, {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    hass.states.async_set(UPSTAIRS_SENSOR, "44.0")
    await hass.async_block_till_done()

    # The revert waits 60 s of sustained idle, so the clock has to move or this
    # test proves nothing at all.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is True
    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET, (
        "a shed handed eco back while the cycle was still outstanding"
    )


# ---------------------------------------------------------------------------
# What it must NOT reach for: a tank a person parked
#
# The reclaim exists to un-park a tank SMART ECO parked. It cannot see who
# wrote the OFF it is looking at, and the block directly above it in the
# control loop says exactly why that matters: "a tank OFF for any other reason
# may well be OFF because a person wants it that way, and guessing there would
# energise an element nobody asked for."
# ---------------------------------------------------------------------------


async def test_it_does_not_energise_a_tank_a_person_switched_off(hass, world):  # noqa: F811
    """Choosing a policy is not asking for heat on a tank you switched off."""
    await setup_both(hass)
    with freeze_time(TRIGGER):
        hass.states.async_set(PV_EXCESS, "on")  # eco permits heating
        await hass.async_block_till_done()
        await hass.services.async_call(
            "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS).state == "off"

        await eco(hass, "Off until target reached")

    assert hass.states.get(UPSTAIRS).state == "off", (
        "the bypass switched on a tank its owner had switched off"
    )


async def test_turning_the_tank_off_while_it_is_active_is_not_reversed(hass, world):  # noqa: F811
    """Otherwise OFF is unreachable until the bound -- you cannot stop it."""
    await blocked(hass)
    await eco(hass, "Off until target reached")
    assert hass.states.get(UPSTAIRS).state != "off"

    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).state == "off", (
        "the reclaim turned the tank straight back on after a manual OFF"
    )


async def test_turning_it_off_cannot_come_back_as_performance(hass, world):  # noqa: F811
    """The 64 C shape, reached by switching the tank OFF.

    set_operation_mode does not touch smart_eco_last_heating_mode on the OFF
    path, so whatever was there -- possibly PERFORMANCE -- is what a reclaim
    would resume. An unbounded performance run is the failure this integration
    has already had once.
    """
    await blocked(hass)
    await hass.services.async_call(
        "water_heater", "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": "performance"}, blocking=True,
    )
    await hass.async_block_till_done()
    await eco(hass, "Off until target reached")

    await hass.services.async_call(
        "water_heater", "turn_off", {"entity_id": UPSTAIRS}, blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).state != "performance", (
        "turning the tank off armed an unbounded performance run"
    )


async def test_a_shed_is_not_mistaken_for_a_satisfied_tank(hass, world):  # noqa: F811
    """A shed leaves the operation mode intact and forces the switch off.

    hvac_action only reads "off" when the OPERATION MODE is off, so a shed
    reads "idle" -- indistinguishable from a tank that reached target. Handing
    eco back there ends the bypass on the say-so of the load balancer.

    Coverage note: two guards cover this -- one stops the 60 s timer arming,
    one stops the revert when it fires. Removing EITHER alone still passes;
    removing both fails. So this pins the outcome and neither guard
    individually, which is what belt-and-braces means.
    """
    from custom_components.generic_water_heater import DOMAIN, SERVICE_SHED

    with freeze_time(TRIGGER):
        await blocked(hass)
        await eco(hass, "Off until target reached")
        await hass.services.async_call(
            DOMAIN, SERVICE_SHED, {"entity_id": UPSTAIRS}, blocking=True,
        )
        await hass.async_block_till_done()

    # Cold tank: it is nowhere near satisfied, only shed. The idle timer can
    # only arm on a control pass AFTER the shed, so drive one, then step well
    # past the 60 s it waits -- otherwise this passes on timing, not on logic.
    with freeze_time(TRIGGER + timedelta(minutes=5)):
        hass.states.async_set(UPSTAIRS_SENSOR, "30.0")
        await hass.async_block_till_done()
    with freeze_time(TRIGGER + timedelta(minutes=7)):
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET, (
        "a load shed was read as 'target reached' and handed eco back"
    )


# ---------------------------------------------------------------------------
# The bound has to be a real bound
# ---------------------------------------------------------------------------


async def test_the_bound_survives_a_restart(hass, world):  # noqa: F811
    """Restarting the clock on every restart extends the bypass without limit.

    Five hours in, a restart must not buy another full window -- and a Core
    upgrade or a crash loop must not be able to hold Smart Eco down for ever
    one 6 h window at a time.
    """
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    started = (TRIGGER - timedelta(hours=5)).isoformat()
    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                "electric",
                {
                    "smart_eco_mode": SMART_ECO_MODE_OFF_UNTIL_TARGET,
                    "smart_eco_previous_mode": SMART_ECO_MODE_AUTO_RESUME,
                    "smart_eco_off_until_target_since": started,
                },
            ),
        ),
    )
    with freeze_time(TRIGGER):
        await setup_both(hass)
        hass.states.async_set(PV_EXCESS, "off")
        await hass.async_block_till_done()
    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET

    # Two more hours -> seven since it actually started, past the 6 h default.
    with freeze_time(TRIGGER + timedelta(hours=2)):
        hass.states.async_set(UPSTAIRS_SENSOR, "30.0")
        await hass.async_block_till_done()

    assert eco_mode(hass) != SMART_ECO_MODE_OFF_UNTIL_TARGET, (
        "the restart reset the bound, so eco stayed down past its window"
    )


# ---------------------------------------------------------------------------
# ASAP has to hold up for the whole life of a cycle, not just its start
# ---------------------------------------------------------------------------


async def test_switching_to_asap_mid_cycle_stands_eco_down(hass, world):  # noqa: F811
    """Upgrading a waiting cycle to ASAP is the obvious thing to reach for.

    _async_evaluate_disinfection returns early while a cycle is in flight, so
    the eco bypass lives only on the start path -- the switch would silently
    do nothing and the cycle would keep waiting for sun.
    """
    upstairs, _ = await setup_with_policy_blocked(hass)
    await report_risk(hass, upstairs, "Elevated")

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert eco_mode(hass) == SMART_ECO_MODE_AUTO_RESUME

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect ASAP"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET, (
        "switching a running cycle to ASAP left it waiting for the eco window"
    )


async def test_cancelling_an_asap_cycle_hands_eco_back(hass, world):  # noqa: F811
    """The bypass was taken out FOR the cycle; ending it should return it.

    Waiting for the 6 h bound is not good enough: a cancelled cycle usually
    leaves the tank OFF, and hvac_action then reads "off" rather than "idle",
    so the satisfied path can never fire and the full window always elapses.
    """
    upstairs, _ = await setup_with_policy_blocked(hass)
    await report_risk(hass, upstairs, "Elevated")
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Disinfect ASAP"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": LEG_SELECT, "option": "Off"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(UPSTAIRS).attributes["disinfection_active"] is False
    assert eco_mode(hass) == SMART_ECO_MODE_AUTO_RESUME, (
        "cancelling the cycle left Smart Eco standing down until the bound"
    )
