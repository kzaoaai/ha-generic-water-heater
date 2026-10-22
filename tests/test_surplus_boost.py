"""The surplus boost: store spare power as heat, never buy it.

While the surplus entity is active and "Boost: Use PV Excess" is on, a tank in
electric heats toward the boost target instead of its normal one. On the
utility or a battery it stops at its normal target. A boost that heated and
lost its surplus within SURPLUS_BOOST_SHORT_RUN is not tried again for
SURPLUS_BOOST_LOCKOUT, because a battery-judged surplus detector sees the
heater's own draw (2026-10-18: six 2-3 minute cycles in an hour).
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from homeassistant.components.water_heater import STATE_ELECTRIC, STATE_PERFORMANCE
from homeassistant.const import STATE_OFF, STATE_ON
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater import (
    CONF_HOT_TOLERANCE,
    DOMAIN,
    SERVICE_SHED,
    SMART_ECO_MODE_OFF,
)
from custom_components.generic_water_heater.config_flow import (
    _apply_cleared_and_defaults,
    _surplus_boost_errors,
)
from custom_components.generic_water_heater.water_heater import (
    SURPLUS_BOOST_LOCKOUT,
    SURPLUS_BOOST_SHORT_RUN,
)
from tests.test_eco_inputs import (
    BOOST,
    SOURCE,
    UPSTAIRS,
    heater,
    set_state,
    setup_inputs,
    switch_on,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    auto_enable_custom_integrations,
    entities,
    world,
)

T0 = datetime(2026, 10, 18, 13, 16, 57, tzinfo=timezone.utc)


async def boosting(hass, *, temperature="52", enabled=True, **kwargs):
    """Normal target 50, boost 61, tank at 52: satisfied unless boosting."""
    await setup_inputs(hass, temperature=temperature, boost=61.0, **kwargs)
    if enabled:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": BOOST}, blocking=True
        )
        await hass.async_block_till_done()


async def test_surplus_heats_past_the_normal_target(hass, world):  # noqa: F811
    await boosting(hass)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)
    assert heater(hass).attributes["surplus_boost_active"] is True
    # The person's own target is still the one published.
    assert heater(hass).attributes["temperature"] == 50


async def test_without_the_switch_surplus_stops_at_the_normal_target(hass, world):  # noqa: F811
    await boosting(hass, enabled=False)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert not switch_on(hass)
    assert heater(hass).attributes["surplus_boost_active"] is False


async def test_the_utility_never_heats_past_the_normal_target(hass, world):  # noqa: F811
    await boosting(hass)
    await set_state(hass, SOURCE, "Utility")
    assert heater(hass).attributes["smart_eco_condition_met"] is True
    assert not switch_on(hass)


async def test_the_boost_stops_when_surplus_leaves(hass, world):  # noqa: F811
    """Utility still carries the house, so eco allows heating - to 50, not 61."""
    await boosting(hass, source="Utility")
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)
    await set_state(hass, PV_EXCESS, STATE_OFF)
    assert not switch_on(hass)


async def test_the_boost_stops_at_its_own_target(hass, world):  # noqa: F811
    await boosting(hass)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)
    # Past it: with zero tolerances the thresholds meet AT the target, where
    # the existing control turns on, so exactly 61 would not show the stop.
    hass.states.async_set(UPSTAIRS_SENSOR, "61.5", {"device_class": "temperature"})
    await hass.async_block_till_done()
    assert not switch_on(hass)


async def test_off_stays_off(hass, world):  # noqa: F811
    """A person's OFF, taken while boosting, holds through surplus coming back."""
    await boosting(hass)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)
    heater_entity = entities(hass)["Upstairs"]
    await heater_entity.async_set_operation_mode(STATE_OFF)
    await set_state(hass, PV_EXCESS, STATE_OFF)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert heater(hass).state == STATE_OFF
    assert not switch_on(hass)
    assert heater(hass).attributes["surplus_boost_active"] is False


async def test_a_shed_outranks_the_boost(hass, world):  # noqa: F811
    await boosting(hass)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)
    await hass.services.async_call(
        DOMAIN, SERVICE_SHED, {"entity_id": UPSTAIRS}, blocking=True
    )
    await hass.async_block_till_done()
    assert not switch_on(hass)
    assert heater(hass).attributes["surplus_boost_active"] is False


async def test_performance_is_not_a_boost(hass, world):  # noqa: F811
    await boosting(hass)
    heater_entity = entities(hass)["Upstairs"]
    await heater_entity.async_set_operation_mode(STATE_PERFORMANCE)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert heater(hass).attributes["surplus_boost_active"] is False


async def test_boost_only_heat_is_eco_gated(hass, world):  # noqa: F811
    """With the policy off, only the boost decides - and the boost needs surplus."""
    await boosting(hass)
    # Surplus first: the gate parked the tank at OFF, and only its own
    # restore branch un-parks it.
    await set_state(hass, PV_EXCESS, STATE_ON)
    heater_entity = entities(hass)["Upstairs"]
    await heater_entity.async_set_smart_eco_mode(SMART_ECO_MODE_OFF, source="smart_eco_select")
    await hass.async_block_till_done()
    assert switch_on(hass)
    assert heater(hass).attributes["eco_gated"] is True


async def test_heat_below_the_normal_target_is_not_boost_gated(hass, world):  # noqa: F811
    """Below its normal target the tank heats on any source with the policy off."""
    await boosting(hass, temperature="45")
    # Surplus first: the gate parked the tank at OFF, and only its own
    # restore branch un-parks it.
    await set_state(hass, PV_EXCESS, STATE_ON)
    heater_entity = entities(hass)["Upstairs"]
    await heater_entity.async_set_smart_eco_mode(SMART_ECO_MODE_OFF, source="smart_eco_select")
    await hass.async_block_till_done()
    assert switch_on(hass)
    assert heater(hass).attributes["eco_gated"] is False


async def test_the_hot_tolerance_bounds_boost_gating(hass, world):  # noqa: F811
    """Inside the normal hysteresis band the element would stay on regardless."""
    await boosting(hass, temperature="51", **{CONF_HOT_TOLERANCE: 2.0})
    # Surplus first: the gate parked the tank at OFF, and only its own
    # restore branch un-parks it.
    await set_state(hass, PV_EXCESS, STATE_ON)
    heater_entity = entities(hass)["Upstairs"]
    await heater_entity.async_set_smart_eco_mode(SMART_ECO_MODE_OFF, source="smart_eco_select")
    await hass.async_block_till_done()
    assert switch_on(hass)
    assert heater(hass).attributes["eco_gated"] is False


async def test_a_short_boost_is_locked_out(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await boosting(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)

        frozen.tick(timedelta(minutes=3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert not switch_on(hass)
        assert heater(hass).attributes["surplus_boost_locked_until"] is not None

        # Surplus is back a minute later; the boost is not.
        frozen.tick(timedelta(minutes=1))
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert heater(hass).attributes["surplus_boost_active"] is False
        assert not switch_on(hass)

        # Once the lockout ends, the boost tries again on the surplus still there.
        frozen.tick(SURPLUS_BOOST_LOCKOUT)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert heater(hass).attributes["surplus_boost_locked_until"] is None
        assert switch_on(hass)


async def test_a_long_boost_is_not_locked_out(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await boosting(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)

        frozen.tick(SURPLUS_BOOST_SHORT_RUN + timedelta(seconds=1))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert heater(hass).attributes["surplus_boost_locked_until"] is None

        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)


async def test_a_short_window_without_boost_heat_is_not_locked_out(hass, world):  # noqa: F811
    """The boost switch was off, so nothing of the boost's caused the cycle."""
    with freeze_time(T0) as frozen:
        await boosting(hass, enabled=False)
        await set_state(hass, PV_EXCESS, STATE_ON)
        frozen.tick(timedelta(minutes=3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert heater(hass).attributes["surplus_boost_locked_until"] is None


async def test_the_lockout_never_stops_normal_heating(hass, world):  # noqa: F811
    """Locked out, a tank below its normal target still heats on surplus."""
    with freeze_time(T0) as frozen:
        await boosting(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        frozen.tick(timedelta(minutes=3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert heater(hass).attributes["surplus_boost_locked_until"] is not None

        hass.states.async_set(UPSTAIRS_SENSOR, "45", {"device_class": "temperature"})
        await hass.async_block_till_done()
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)
        assert heater(hass).state == STATE_ELECTRIC


def test_a_boost_needs_a_surplus_entity():
    flat = _apply_cleared_and_defaults({"surplus_boost": {"surplus_boost_target_temperature": 61}})
    assert _surplus_boost_errors(flat) == {"base": "surplus_boost_needs_surplus_entity"}


def test_a_boost_above_the_maximum_is_refused():
    flat = _apply_cleared_and_defaults(
        {
            "temperatures": {"max_temp": 60},
            "smart_eco": {"surplus_entity": "binary_sensor.excess"},
            "surplus_boost": {"surplus_boost_target_temperature": 61},
        }
    )
    assert _surplus_boost_errors(flat) == {"base": "surplus_boost_above_max_temp"}


def test_no_boost_target_is_no_boost_and_no_error():
    flat = _apply_cleared_and_defaults({"smart_eco": {}, "surplus_boost": {}})
    assert flat["surplus_boost_target_temperature"] is None
    assert flat["surplus_entity"] == ""
    assert flat["utility_entity"] == ""
    assert flat["surplus_state"] == "on"
    assert _surplus_boost_errors(flat) == {}


async def test_a_boost_target_below_the_normal_one_never_lowers_it(hass, world):  # noqa: F811
    """Aiming at the boost target must only ever add heat."""
    await setup_inputs(hass, temperature="47", boost=45.0, utility=False)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": BOOST}, blocking=True
    )
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert heater(hass).attributes["surplus_boost_active"] is False
    assert switch_on(hass)


async def test_with_the_policy_off_surplus_still_starts_the_boost(hass, world):  # noqa: F811
    """No policy to re-run control on a surplus edge; the boost must do it."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                STATE_ELECTRIC,
                {"smart_eco_mode": SMART_ECO_MODE_OFF, "surplus_boost_enabled": True},
            ),
        ),
    )
    await setup_inputs(hass, temperature="52", boost=61.0)
    assert not switch_on(hass)
    await set_state(hass, PV_EXCESS, STATE_ON)
    assert switch_on(hass)


async def test_a_slow_switch_still_counts_as_boost_heat(hass, world):  # noqa: F811
    """A relay reporting its ON late is still the boost's draw.

    The control pass that commanded it saw the switch still off; only the
    reading taken when surplus leaves sees it on.
    """
    from tests.test_startup_grace import delayed_switch

    with freeze_time(T0) as frozen:
        delayed_switch(hass, world, latency=5)
        await boosting(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert not switch_on(hass)
        frozen.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert switch_on(hass)

        frozen.tick(timedelta(minutes=3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert heater(hass).attributes["surplus_boost_locked_until"] is not None
        # Let the late OFF land, so no timer outlives the test.
        frozen.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_a_boost_that_finished_before_surplus_left_is_not_locked_out(hass, world):  # noqa: F811
    """At its target with the element off, the boost did not drain the surplus."""
    with freeze_time(T0) as frozen:
        await boosting(hass)
        await set_state(hass, PV_EXCESS, STATE_ON)
        assert switch_on(hass)
        hass.states.async_set(UPSTAIRS_SENSOR, "61.5", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert not switch_on(hass)

        frozen.tick(timedelta(minutes=3))
        await set_state(hass, PV_EXCESS, STATE_OFF)
        assert heater(hass).attributes["surplus_boost_locked_until"] is None
