"""A wall-switch ON that promotes the tank to performance ends by itself.

Flipping the switch at the wall while electric would idle means "heat now", so
the heater promotes the mode to performance. Before this, nothing ever ended
that run: performance holds the element on without reading the temperature, no
Smart Eco pause or timer touches the operation mode, and the promotion was
recorded as the heating mode the eco gate restores, so it came back after every
park. One press of a button became an unbounded performance run, this
integration's worst failure mode. It now reverts to electric after
smart_eco_manual_off_resume_hours.
"""

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.generic_water_heater import SMART_ECO_MODE_OFF
from tests.test_disinfection import choose, report_risk
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"
HOT = "70"  # above the 60 C target: electric would idle, so a wall ON promotes


async def hot_tank(hass, *, pv=STATE_ON, **entry_kwargs):
    """Both entries loaded, the upstairs tank above target, eco permitting."""
    hass.states.async_set(UPSTAIRS_SENSOR, HOT, {"device_class": "temperature"})
    hass.states.async_set(PV_EXCESS, pv)
    await setup_both(hass, stagger_seconds=0.0, **entry_kwargs)
    return entities(hass)["Upstairs"]


async def wall_on(hass):
    """A person turns the element on at the switch itself."""
    hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
    await hass.async_block_till_done()


async def past(hass, upstairs):
    """Move past the run's deadline (the manual pause ends at the same time)."""
    until = upstairs._manual_performance_until
    assert until is not None, "test premise: a bounded run is in progress"
    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()


def attrs(hass):
    return hass.states.get(UPSTAIRS).attributes


async def test_a_wall_flip_while_electric_idles_still_forces_heat(hass, world):  # noqa: F811
    """The feature is kept: heat now, with a published deadline."""
    upstairs = await hot_tank(hass)
    assert upstairs.state == STATE_ELECTRIC
    assert not upstairs._electric_mode_wants_heating(), "test premise"

    await wall_on(hass)

    assert upstairs.state == STATE_PERFORMANCE
    until = dt_util.parse_datetime(attrs(hass)["manual_performance_until"])
    expected = dt_util.utcnow() + timedelta(
        hours=upstairs._smart_eco_manual_off_resume_hours
    )
    assert abs((until - expected).total_seconds()) < 60
    assert attrs(hass)["smart_eco_last_heating_mode"] == STATE_ELECTRIC, (
        "the flip was recorded as the mode the eco gate restores"
    )


async def test_the_run_ends_at_its_deadline(hass, world):  # noqa: F811
    """From electric: back to electric, and a hot tank's element goes off."""
    upstairs = await hot_tank(hass)
    await wall_on(hass)
    assert upstairs.state == STATE_PERFORMANCE

    await past(hass, upstairs)

    assert upstairs.state == STATE_ELECTRIC, "performance outlived its bound"
    assert hass.states.get(UPSTAIRS_SWITCH).state == STATE_OFF
    assert attrs(hass)["manual_performance_until"] is None
    assert upstairs._manual_performance_timer is None


async def test_a_flip_on_a_tank_the_eco_gate_parked_is_bounded_too(hass, world):  # noqa: F811
    """From OFF: the other promotion site ends on its deadline as well.

    The eco condition comes back during the run, so when the manual pause ends
    (at the same instant as the deadline) eco does not park the tank again.
    Without that, a re-park would hide a run that never ended.
    """
    upstairs = await hot_tank(hass, pv=STATE_OFF)
    assert upstairs.state == STATE_OFF, "test premise: eco parked the tank"

    await wall_on(hass)
    assert upstairs.state == STATE_PERFORMANCE
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()

    await past(hass, upstairs)
    assert upstairs.state == STATE_ELECTRIC, "performance outlived its bound"


async def test_after_the_run_the_eco_gate_restores_electric(hass, world):  # noqa: F811
    """The flip is not recorded as the mode a later eco restore brings back."""
    upstairs = await hot_tank(hass, pv=STATE_OFF)
    await wall_on(hass)
    assert upstairs.state == STATE_PERFORMANCE

    await past(hass, upstairs)
    assert upstairs.state == STATE_OFF, "test premise: eco parked it again"

    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    assert upstairs.state == STATE_ELECTRIC, (
        "the eco gate restored performance after the run had ended"
    )


async def test_an_eco_park_mid_run_drops_the_deadline(hass, world):  # noqa: F811
    """Parked, the run is over; its deadline must not stay published."""
    upstairs = await hot_tank(hass, pv=STATE_OFF)
    await wall_on(hass)
    assert attrs(hass)["manual_performance_until"] is not None

    # A person choosing the policy again ends the manual pause, so eco, still
    # blocked, parks the tank at once.
    await upstairs.async_set_smart_eco_mode(upstairs._smart_eco_mode, source="test")
    await hass.async_block_till_done()

    assert upstairs.state == STATE_OFF, "test premise: eco parked it"
    assert attrs(hass)["manual_performance_until"] is None
    assert upstairs._manual_performance_timer is None


async def test_the_bound_holds_with_no_smart_eco_timer_at_all(hass, world):  # noqa: F811
    """Smart Eco off: no pause and no countdown, and still a bound.

    Ending the run when the manual pause ends would miss exactly this case.
    """
    upstairs = await hot_tank(hass)
    await upstairs.async_set_smart_eco_mode(SMART_ECO_MODE_OFF, source="test")
    await hass.async_block_till_done()

    await wall_on(hass)
    assert upstairs.state == STATE_PERFORMANCE
    assert upstairs._smart_eco_pause_reason is None, "test premise: no eco pause"

    await past(hass, upstairs)
    assert upstairs.state == STATE_ELECTRIC


async def test_the_control_pass_ends_an_overdue_run_without_the_timer(hass, world):  # noqa: F811
    """The timer is not the only bound: every control pass checks the deadline.

    Pins the check at the top of _async_control_heating. With the timer
    working the two are redundant, so the timer is dropped here on purpose.
    """
    upstairs = await hot_tank(hass)
    await wall_on(hass)
    until = upstairs._manual_performance_until
    upstairs._manual_performance_timer()
    upstairs._manual_performance_timer = None

    with freeze_time(until + timedelta(seconds=1)):
        hass.states.async_set(UPSTAIRS_SENSOR, "71", {"device_class": "temperature"})
        await hass.async_block_till_done()

    assert upstairs.state == STATE_ELECTRIC


async def test_performance_a_person_chooses_is_not_bounded(hass, world):  # noqa: F811
    """Only the wall flip's guess is bounded, never an explicit choice."""
    upstairs = await hot_tank(hass)
    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_PERFORMANCE},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert attrs(hass)["manual_performance_until"] is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=2))
    await hass.async_block_till_done()
    assert upstairs.state == STATE_PERFORMANCE


async def test_choosing_a_mode_replaces_the_run_and_its_deadline(hass, world):  # noqa: F811
    upstairs = await hot_tank(hass)
    await wall_on(hass)

    await hass.services.async_call(
        "water_heater",
        "set_operation_mode",
        {"entity_id": UPSTAIRS, "operation_mode": STATE_PERFORMANCE},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert attrs(hass)["manual_performance_until"] is None
    assert upstairs._manual_performance_timer is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=2))
    await hass.async_block_till_done()
    assert upstairs.state == STATE_PERFORMANCE, (
        "a deadline from the earlier flip ended a performance run chosen since"
    )


async def test_a_disinfection_cycle_is_not_cut_short_by_the_bound(hass, world):  # noqa: F811
    """A cycle that starts during the run owns the performance from then on."""
    upstairs = await hot_tank(hass, enable_legionella_sensor=True)
    upstairs_entry = hass.config_entries.async_get_entry(upstairs._entry_id)
    await wall_on(hass)
    assert upstairs.state == STATE_PERFORMANCE
    until = upstairs._manual_performance_until

    await report_risk(hass, upstairs_entry, "Elevated")
    await choose(hass, "Once (eco)")
    assert upstairs._disinfecting

    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert upstairs._disinfecting
    assert upstairs.state == STATE_PERFORMANCE, "the bound ended a disinfection cycle"


async def test_a_flip_during_a_parked_disinfection_cycle_leaves_the_cycle_alone(hass, world):  # noqa: F811
    """The cycle owns its performance and the mode the eco gate restores.

    A cycle parked by eco carries performance across the gap in
    smart_eco_last_heating_mode. A flip must not overwrite that with electric,
    or the cycle comes back in electric, stops at target and never holds.
    """
    upstairs = await hot_tank(hass, pv=STATE_OFF, enable_legionella_sensor=True)
    upstairs_entry = hass.config_entries.async_get_entry(upstairs._entry_id)
    await report_risk(hass, upstairs_entry, "Elevated")
    await choose(hass, "Once (eco)")
    assert upstairs._disinfecting and upstairs.state == STATE_OFF, "test premise"

    await wall_on(hass)

    assert upstairs._disinfecting
    assert attrs(hass)["smart_eco_last_heating_mode"] == STATE_PERFORMANCE
    assert attrs(hass)["manual_performance_until"] is None


async def test_the_deadline_survives_a_restart(hass, world):  # noqa: F811
    """Restored, not restarted: a restart must not extend the run."""
    until = dt_util.utcnow() + timedelta(minutes=20)
    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                STATE_PERFORMANCE,
                {"temperature": 60, "manual_performance_until": until.isoformat()},
            ),
        ),
    )
    upstairs = await hot_tank(hass)
    assert upstairs.state == STATE_PERFORMANCE
    assert upstairs._manual_performance_until == until

    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert upstairs.state == STATE_ELECTRIC


async def test_a_run_already_overdue_at_a_restart_ends_at_once(hass, world):  # noqa: F811
    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                STATE_PERFORMANCE,
                {
                    "temperature": 60,
                    "manual_performance_until": (
                        dt_util.utcnow() - timedelta(minutes=1)
                    ).isoformat(),
                },
            ),
        ),
    )
    upstairs = await hot_tank(hass)
    assert upstairs.state == STATE_ELECTRIC
    assert attrs(hass)["manual_performance_until"] is None
