"""Heat that depends on the surplus has to fit it (4.7.0).

Two heaters, 1850 W upstairs and 1190 W downstairs, share one surplus entity.
Until 4.7.0 both started on it a stagger apart whatever it could carry:

- 2026-11-23 13:20: 3.8 kW of sun and a house that rose to 1.3 kW. Both ran,
  the pack gave 300 to 600 W, and the detector took the surplus away from both
  after 21 minutes with the tanks at 29 and 31 C.
- 2026-11-21 12:43: 2.8 kW of sun. Both ran for 5 minutes, sat out a 30 minute
  lockout with the array throttled to the house load, and ran for 3 more.

With a surplus power sensor the heaters start one at a time and only if they
fit, the tank closest to its goal first, and when the surplus goes short
the last one started stops and the others carry on. Without the sensor nothing
here runs.

The fleet's own logic is unit-tested in test_fleet_surplus.py; this file runs
the two real config entries against a small model of the supply.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from homeassistant.components.water_heater import STATE_ELECTRIC
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import CoreState, State
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.generic_water_heater import (
    CONF_ECO_TEMPLATE,
    CONF_POWER_SENSOR,
    CONF_SURPLUS_BOOST_TARGET,
    CONF_SURPLUS_ENTITY,
    CONF_SURPLUS_POWER_ENTITY,
    CONF_SURPLUS_STATE,
    CONF_UTILITY_ENTITY,
    CONF_UTILITY_STATE,
    SMART_ECO_MODE_OFF,
)
from custom_components.generic_water_heater.config_flow import (
    _apply_cleared_and_defaults,
    _surplus_power_errors,
)
from custom_components.generic_water_heater.water_heater_const import (
    STARTUP_GRACE,
    SURPLUS_BOOST_LOCKOUT,
    SURPLUS_FIT_LOCKOUT,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    DOWNSTAIRS_SENSOR,
    DOWNSTAIRS_SWITCH,
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    commanded,
    entities,
    world,
)

SPARE = "sensor.pv_surplus_power"
SOURCE = "sensor.home_power_source"
UP_METER = "sensor.upstairs_power"
DOWN_METER = "sensor.downstairs_power"
UPSTAIRS = "water_heater.upstairs"
DOWNSTAIRS = "water_heater.downstairs"
UP_W = 1850
DOWN_W = 1190
WAITING = "Waiting for more surplus"

T0 = datetime(2026, 11, 23, 11, 20, 0, tzinfo=timezone.utc)


class Supply:
    """A small model of the inverter's surplus power sensor.

    `pv` is what the array could make and `house` the load without the heaters.
    With the pack full it takes nothing, so a positive balance is power the
    array is not making: the sensor reads 0 and `curtailed`. Otherwise the pack
    takes or gives the balance and the sensor reads it. Meters follow relays.
    """

    def __init__(self, hass, *, pv, house, full=True):
        self.hass = hass
        self.pv = pv
        self.house = house
        self.full = full
        self.available = True
        self.temps = {}

    def warm(self, seconds):
        """A heated tank's reading climbs (4 C an hour here). One that sat
        still for half an hour of heating would be held off as a stale reading,
        which is another test's subject."""
        for switch, sensor in (
            (UPSTAIRS_SWITCH, UPSTAIRS_SENSOR),
            (DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR),
        ):
            if self.hass.states.get(switch).state != STATE_ON:
                continue
            now = self.temps.get(sensor, float(self.hass.states.get(sensor).state))
            self.temps[sensor] = now + seconds * 4 / 3600
            self.hass.states.async_set(
                sensor, f"{self.temps[sensor]:.2f}", {"device_class": "temperature"}
            )

    def publish(self):
        draw = 0
        for switch, meter, watts in (
            (UPSTAIRS_SWITCH, UP_METER, UP_W),
            (DOWNSTAIRS_SWITCH, DOWN_METER, DOWN_W),
        ):
            on = self.hass.states.get(switch).state == STATE_ON
            draw += watts if on else 0
            self.hass.states.async_set(
                meter,
                str(watts if on else 0),
                {"unit_of_measurement": "W", "device_class": "power"},
            )
        balance = self.pv - self.house - draw
        curtailed = self.full and balance > 0
        spare = 0 if curtailed else balance
        self.hass.states.async_set(
            SPARE,
            str(int(round(spare / 100) * 100)) if self.available else STATE_UNAVAILABLE,
            {
                "unit_of_measurement": "W",
                "device_class": "power",
                "curtailed": curtailed,
            },
        )


async def run(hass, frozen, supply, seconds, step=5):
    """Let `seconds` pass, the supply reporting every `step`."""
    for _ in range(int(seconds / step)):
        supply.publish()
        supply.warm(step)
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=step))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    supply.publish()
    await hass.async_block_till_done()


async def setup_pair(
    hass,
    *,
    up_temp="28.8",
    down_temp="30.6",
    power=True,
    source="Battery",
    stagger=60.0,
    **extra,
):
    """Both real entries: surplus and utility entities, no template, meters."""
    hass.states.async_set(UPSTAIRS_SENSOR, up_temp, {"device_class": "temperature"})
    hass.states.async_set(DOWNSTAIRS_SENSOR, down_temp, {"device_class": "temperature"})
    hass.states.async_set(SOURCE, source)
    hass.states.async_set(SPARE, STATE_UNAVAILABLE)
    for meter in (UP_METER, DOWN_METER):
        hass.states.async_set(
            meter, "0", {"unit_of_measurement": "W", "device_class": "power"}
        )
    common = {
        CONF_ECO_TEMPLATE: "",
        CONF_SURPLUS_ENTITY: PV_EXCESS,
        CONF_SURPLUS_STATE: "on",
        CONF_UTILITY_ENTITY: SOURCE,
        CONF_UTILITY_STATE: "Utility",
        **extra,
    }
    if power:
        common[CONF_SURPLUS_POWER_ENTITY] = SPARE
    upstairs = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, stagger, target_temp=48.0,
        **common, **{CONF_POWER_SENSOR: UP_METER},
    )
    downstairs = build_entry(
        "Downstairs", DOWNSTAIRS_SWITCH, DOWNSTAIRS_SENSOR, stagger, target_temp=45.0,
        **common, **{CONF_POWER_SENSOR: DOWN_METER},
    )
    for entry in (upstairs, downstairs):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return upstairs, downstairs


def relay(hass, switch):
    return hass.states.get(switch).state


def label(hass, entity_id):
    return hass.states.get(entity_id).attributes["smart_eco_state"]


def ons(calls, switch):
    return commanded(calls["turn_on"]).count(switch)


def offs(calls, switch):
    return commanded(calls["turn_off"]).count(switch)


async def surplus_arrives(hass, frozen, supply, seconds, step=5):
    supply.publish()
    hass.states.async_set(PV_EXCESS, STATE_ON)
    await hass.async_block_till_done()
    await run(hass, frozen, supply, seconds, step)


# --- the two days -------------------------------------------------------------


async def test_2026_11_23_one_heater_goes_and_the_other_keeps_the_surplus(hass, world):  # noqa: F811
    """3.8 kW of sun on a full pack. Downstairs is the closer to its goal (14.4 C
    against 19.2) and starts; upstairs follows a minute later because the array
    is held back and there is room. Then the house rises to 1.3 kW: the pack
    gives 540 W, upstairs (the last one started) stops after a minute and
    downstairs heats on toward its target."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        world["turn_on"].clear()
        world["turn_off"].clear()

        await surplus_arrives(hass, frozen, supply, 30)
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        assert label(hass, UPSTAIRS) == WAITING

        await run(hass, frozen, supply, 150)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON       # 110 W to spare

        supply.house = 1300
        await run(hass, frozen, supply, 55)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON       # not yet a minute
        await run(hass, frozen, supply, 15)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        assert label(hass, UPSTAIRS) == WAITING
        assert label(hass, DOWNSTAIRS) == "Heating in eco"
        # While its wait runs the surplus does not count for upstairs at all,
        # so nothing it does is heat on the surplus.
        assert not entities(hass)["Upstairs"]._heat_depends_on_surplus()

        # A long time later downstairs has not been touched.
        await run(hass, frozen, supply, 600, step=20)
        assert ons(world, DOWNSTAIRS_SWITCH) == 1 and offs(world, DOWNSTAIRS_SWITCH) == 0
        assert ons(world, UPSTAIRS_SWITCH) == 1 and offs(world, UPSTAIRS_SWITCH) == 1


async def test_2026_11_23_without_the_power_sensor_both_run_into_the_shortfall(hass, world):  # noqa: F811
    """The same supply as shipped until 4.7.0, and as it still is with the
    option unset: both start a stagger apart and neither gives way. (The
    detector then takes the surplus from both; that part is the detector's.)"""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, power=False)
        supply = Supply(hass, pv=3800, house=1300)
        world["turn_on"].clear()
        world["turn_off"].clear()
        await surplus_arrives(hass, frozen, supply, 400)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        assert world["turn_off"] == []


async def test_2026_11_21_the_second_heater_is_tried_once_and_the_first_heats_on(hass, world):  # noqa: F811
    """2.8 kW of sun, 0.8 kW of house, a full pack. On the day both heaters got
    8 minutes in 40. Here downstairs heats throughout; upstairs is tried once
    (the array is held back, so nothing says it will not fit), costs the pack a
    minute at 1 kW, and waits the full lockout of a short run."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=2800, house=800)
        world["turn_on"].clear()
        world["turn_off"].clear()
        await surplus_arrives(hass, frozen, supply, 200)

        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        assert ons(world, UPSTAIRS_SWITCH) == 1 and offs(world, UPSTAIRS_SWITCH) == 1
        locked = dt_util.parse_datetime(
            hass.states.get(UPSTAIRS).attributes["surplus_boost_locked_until"]
        )
        remaining = locked - dt_util.utcnow()
        assert SURPLUS_FIT_LOCKOUT < remaining <= SURPLUS_BOOST_LOCKOUT

        await run(hass, frozen, supply, 1500, step=30)
        assert offs(world, DOWNSTAIRS_SWITCH) == 0
        assert ons(world, UPSTAIRS_SWITCH) == 1            # still inside the lockout

        # It ends, the array is still held back, and upstairs is tried again.
        await run(hass, frozen, supply, 300, step=10)
        assert ons(world, UPSTAIRS_SWITCH) == 2
        assert offs(world, DOWNSTAIRS_SWITCH) == 0


async def test_a_heater_overtaken_after_a_long_run_waits_the_shorter_time(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 900, step=15)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        supply.house = 1300
        await run(hass, frozen, supply, 90)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        locked = dt_util.parse_datetime(
            hass.states.get(UPSTAIRS).attributes["surplus_boost_locked_until"]
        )
        assert locked - dt_util.utcnow() <= SURPLUS_FIT_LOCKOUT


async def test_a_failed_start_waits_twice_as_long_the_second_time(hass, world):  # noqa: F811
    """The 2026-11-21 supply again. A full pack goes on saying more is there
    without saying how much, so upstairs is tried, fails, and is tried again
    when its wait ends. The second wait is an hour, not half; a new surplus
    starts the count again."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=2800, house=800)
        await surplus_arrives(hass, frozen, supply, 200)
        heater = entities(hass)["Upstairs"]
        assert heater._surplus_fit_failures == 1

        await run(hass, frozen, supply, 1900, step=20)      # second try, second failure
        assert ons(world, UPSTAIRS_SWITCH) == 2 and offs(world, UPSTAIRS_SWITCH) == 2
        assert heater._surplus_fit_failures == 2
        locked = dt_util.parse_datetime(
            hass.states.get(UPSTAIRS).attributes["surplus_boost_locked_until"]
        )
        assert SURPLUS_BOOST_LOCKOUT < locked - dt_util.utcnow() <= 2 * SURPLUS_BOOST_LOCKOUT
        assert offs(world, DOWNSTAIRS_SWITCH) == 0

        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await hass.async_block_till_done()
        assert heater._surplus_fit_failures == 0


async def test_with_a_minimum_on_time_only_the_last_heater_is_told_to_go(hass, world):  # noqa: F811
    """Found in review. The relay of the heater told to go stays closed for its
    minimum on-time, so the reading stays short. Its claim used to be dropped
    at once, and a settle later the fit sent the other heater off after it:
    both parked for a shortfall one of them could have carried."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, min_on_duration={"seconds": 600})
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 180)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        world["turn_off"].clear()

        supply.house = 1300
        await run(hass, frozen, supply, 300)
        # Told to go, and still on: its ten minutes are not up.
        assert label(hass, UPSTAIRS) == WAITING
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        assert label(hass, DOWNSTAIRS) == "Heating in eco"
        fleet = entities(hass)["Upstairs"]._fleet
        up_id = entities(hass)["Upstairs"]._entry_id
        assert fleet.surplus_releasing(up_id)
        # Its relay reports an attribute, as relays do every few seconds. It
        # is still counted: the load is still there.
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON, {"rssi": -61})
        await hass.async_block_till_done()
        assert fleet.surplus_releasing(up_id)
        # Told once: it has not been told again for the load it still is.
        assert entities(hass)["Upstairs"]._surplus_fit_failures == 1

        await run(hass, frozen, supply, 400, step=10)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        assert commanded(world["turn_off"]) == [UPSTAIRS_SWITCH]
        assert label(hass, DOWNSTAIRS) == "Heating in eco"


async def test_one_heater_alone_rides_out_a_kettle(hass, world):  # noqa: F811
    """With nobody else on the surplus there is nobody to save by going early.
    Two minutes of kettle cost the pack a little and the tank nothing; a load
    that stays is answered."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, down_temp="60")              # downstairs is satisfied
        supply = Supply(hass, pv=3200, house=650)
        await surplus_arrives(hass, frozen, supply, 120)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        world["turn_off"].clear()

        supply.house = 2650                                  # the kettle
        await run(hass, frozen, supply, 120)
        supply.house = 650
        await run(hass, frozen, supply, 120)
        assert world["turn_off"] == []

        supply.house = 2650                                  # and one that stays
        await run(hass, frozen, supply, 170)
        assert commanded(world["turn_off"]) == [UPSTAIRS_SWITCH]


# --- who goes first, and who fits ------------------------------------------


async def test_the_tank_closest_to_its_goal_goes_first(hass, world):  # noqa: F811
    """Downstairs at 30.6 C against 45 has 14.4 to go, upstairs at 28.8 against
    48 has 19.2. Downstairs starts, whichever listener Home Assistant calls
    first: one tank at its target before two that are warm."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        world["turn_on"].clear()
        await surplus_arrives(hass, frozen, supply, 30)
        assert commanded(world["turn_on"]) == [DOWNSTAIRS_SWITCH]


async def test_the_order_follows_the_tanks_not_the_heaters(hass, world):  # noqa: F811
    """With upstairs at 41 C it is the one with less to go, and it starts."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, up_temp="41")
        supply = Supply(hass, pv=3800, house=650)
        world["turn_on"].clear()
        await surplus_arrives(hass, frozen, supply, 30)
        assert commanded(world["turn_on"]) == [UPSTAIRS_SWITCH]


async def test_a_tank_short_of_its_target_is_ranked_before_one_that_only_boosts(hass, world):  # noqa: F811
    """Boost target 65 on both. Downstairs at 50 C already has its normal 45
    and is 15 from its boost target; upstairs at 28.8 is 19.2 from hot water.
    Upstairs is ranked on its normal target and goes first."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, down_temp="50", **{CONF_SURPLUS_BOOST_TARGET: 65.0})
        for name in ("Upstairs", "Downstairs"):
            await entities(hass)[name].async_set_surplus_boost_enabled(True)
        supply = Supply(hass, pv=3800, house=650)
        world["turn_on"].clear()
        await surplus_arrives(hass, frozen, supply, 30)
        assert commanded(world["turn_on"]) == [UPSTAIRS_SWITCH]
        # Upstairs is ranked on its normal target although it too would go on
        # to 65; downstairs on what is left to its boost target, and as extra.
        assert entities(hass)["Upstairs"]._surplus_distance() == (pytest.approx(19.2, abs=0.1), False)
        assert entities(hass)["Downstairs"]._surplus_distance() == (pytest.approx(15.0), True)


async def test_a_known_surplus_too_small_for_one_heater_goes_to_the_one_it_fits(hass, world):  # noqa: F811
    """The pack is still charging at 1.4 kW, so the number is the whole story.
    Upstairs is the closer to its goal (4 C to go) but draws 1850 W and cannot
    use it; downstairs (1190 W) can. The surplus is not left idle."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, up_temp="44")
        entities(hass)["Upstairs"]._element_draw_w = float(UP_W)
        entities(hass)["Downstairs"]._element_draw_w = float(DOWN_W)
        supply = Supply(hass, pv=2000, house=600, full=False)
        world["turn_on"].clear()
        await surplus_arrives(hass, frozen, supply, 200)
        assert commanded(world["turn_on"]) == [DOWNSTAIRS_SWITCH]
        assert label(hass, UPSTAIRS) == WAITING
        reason = hass.states.get(UPSTAIRS).attributes["fleet_hold_reason"]
        assert reason == "surplus: not enough to carry this heater"
        # Waiting on the fit, the tank is idle: eco_gated says its heat would
        # hang on the eco condition, and nothing is drawing to be discounted.
        assert hass.states.get(UPSTAIRS).attributes["hvac_action"] == "idle"

        # The sun climbs: now there is room for it as well.
        supply.pv = 4400
        await run(hass, frozen, supply, 90)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON


async def test_a_heater_that_has_never_run_is_taken_for_a_typical_element(hass, world):  # noqa: F811
    """No draw learned yet: 2000 W is assumed, so 1.4 kW is not enough for
    either, though downstairs would in fact fit."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=2000, house=600, full=False)
        world["turn_on"].clear()
        await surplus_arrives(hass, frozen, supply, 200)
        assert world["turn_on"] == []
        assert label(hass, UPSTAIRS) == WAITING and label(hass, DOWNSTAIRS) == WAITING


async def test_the_draw_is_learned_from_the_heaters_own_meter(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=6000, house=600)
        await surplus_arrives(hass, frozen, supply, 200)
        assert entities(hass)["Upstairs"]._element_draw_w == UP_W
        assert entities(hass)["Downstairs"]._element_draw_w == 1200   # 1190 to 50 W
        # Published with the heater's next write, for the restore.
        hass.states.async_set(UPSTAIRS_SENSOR, "29.5", {"device_class": "temperature"})
        await hass.async_block_till_done()
        assert hass.states.get(UPSTAIRS).attributes["element_draw_w"] == UP_W


async def test_the_learned_draw_survives_a_restart(hass, world):  # noqa: F811
    mock_restore_cache(
        hass, (State(UPSTAIRS, STATE_ELECTRIC, {"element_draw_w": 1850.0}),)
    )
    await setup_pair(hass)
    assert entities(hass)["Upstairs"]._element_draw_w == 1850.0
    assert entities(hass)["Downstairs"]._element_draw_w is None


@pytest.mark.parametrize("stored", [True, "1850", -5, 0, None])
async def test_a_stored_draw_that_is_not_a_positive_number_is_dropped(hass, world, stored):  # noqa: F811
    mock_restore_cache(
        hass, (State(UPSTAIRS, STATE_ELECTRIC, {"element_draw_w": stored}),)
    )
    await setup_pair(hass)
    assert entities(hass)["Upstairs"]._element_draw_w is None


# --- what the fit never touches ------------------------------------------------


async def test_heat_the_utility_allows_is_never_sized(hass, world):  # noqa: F811
    """The utility carries the house and "Eco: Use Utility" is on: this heat
    would go on without the surplus, so the surplus power has no say in it."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass, source="Utility")
        supply = Supply(hass, pv=500, house=3000, full=False)
        await run(hass, frozen, supply, 150)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await run(hass, frozen, supply, 300)
        assert world["turn_off"] == []
        assert not entities(hass)["Upstairs"]._heat_depends_on_surplus()


async def test_heat_a_person_asked_for_is_never_sized(hass, world):  # noqa: F811
    """The ON button is the eco bypass: it stands the policy down until the
    tank is satisfied, so that heat no longer hangs on the surplus."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=500, house=600, full=False)
        await surplus_arrives(hass, frozen, supply, 150)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF      # does not fit
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await run(hass, frozen, supply, 300)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        assert offs(world, UPSTAIRS_SWITCH) == 0


async def test_a_heater_running_for_a_person_is_not_the_one_released(hass, world):  # noqa: F811
    """Upstairs is on because someone pressed ON; downstairs is then tried on
    the surplus and does not fit beside it. The shortfall is downstairs's to
    answer: upstairs's heat was never the surplus's to take back."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=2500, house=600)
        await run(hass, frozen, supply, 10)
        await hass.services.async_call(
            "water_heater", "turn_on", {"entity_id": UPSTAIRS}, blocking=True
        )
        await run(hass, frozen, supply, 30)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        heater = entities(hass)["Upstairs"]
        world["turn_on"].clear()
        world["turn_off"].clear()

        await surplus_arrives(hass, frozen, supply, 200)
        assert not heater._heat_depends_on_surplus()
        assert not heater._fleet.holds_surplus(heater._entry_id)
        assert ons(world, DOWNSTAIRS_SWITCH) == 1 and offs(world, DOWNSTAIRS_SWITCH) == 1
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        assert offs(world, UPSTAIRS_SWITCH) == 0


async def test_heat_the_utility_took_over_is_no_longer_the_surplus_to_take_back(hass, world):  # noqa: F811
    """Both heaters start on the surplus; then the utility arrives and carries
    the house. Their heat now goes on whatever the surplus does, so a short
    reading releases nobody: a release would only set a lockout on a heater
    it cannot stop, and that lockout would bite when the utility left."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 180)
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        fleet = entities(hass)["Upstairs"]._fleet
        assert fleet.surplus_snapshot()["claims"]

        hass.states.async_set(SOURCE, "Utility")
        await hass.async_block_till_done()
        assert fleet.surplus_snapshot()["claims"] == {}
        supply.house = 2500
        world["turn_off"].clear()
        await run(hass, frozen, supply, 200)
        assert world["turn_off"] == []
        for entity_id in (UPSTAIRS, DOWNSTAIRS):
            assert hass.states.get(entity_id).attributes["surplus_boost_locked_until"] is None


async def test_a_heater_that_stops_wanting_heat_does_not_hold_its_sibling_back(hass, world):  # noqa: F811
    """Both ask; downstairs is the closer and would go first. Before anyone is
    admitted its target is lowered under the tank's temperature. Upstairs must
    not keep yielding to a request nobody is making any more."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=6000, house=600, full=False)
        supply.publish()
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert label(hass, DOWNSTAIRS) == WAITING          # inside the request window
        await hass.services.async_call(
            "water_heater", "set_temperature",
            {"entity_id": DOWNSTAIRS, "temperature": 20}, blocking=True,
        )
        world["turn_on"].clear()
        await run(hass, frozen, supply, 20)
        assert commanded(world["turn_on"]) == [UPSTAIRS_SWITCH]


async def test_the_first_report_after_a_switch_on_is_not_the_draw(hass, world):  # noqa: F811
    """A meter's first sample after the relay closes can cover part of an
    interval. Twenty seconds later it is the element."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=6000, house=600)
        supply.publish()
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        for _ in range(2):
            frozen.tick(timedelta(seconds=2))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        heater = entities(hass)["Downstairs"]

        def meter(value, unit="W"):
            hass.states.async_set(
                DOWN_METER, value, {"unit_of_measurement": unit, "device_class": "power"}
            )

        frozen.tick(timedelta(seconds=5))
        meter("600")
        await hass.async_block_till_done()
        assert heater._element_draw_w is None
        frozen.tick(timedelta(seconds=20))
        meter("1.19", "kW")                                 # and in kW
        await hass.async_block_till_done()
        assert heater._element_draw_w == 1200               # to the nearest 50 W
        # A sag of under a tenth does not rewrite it.
        frozen.tick(timedelta(seconds=20))
        meter("1.11", "kW")
        await hass.async_block_till_done()
        assert heater._element_draw_w == 1200


async def test_an_unreadable_power_sensor_changes_nothing(hass, world):  # noqa: F811
    """The surplus entity has always been enough to heat on. A power sensor
    that drops out must not turn that into never heating."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=1300)
        supply.available = False
        await surplus_arrives(hass, frozen, supply, 400)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_ON
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        assert world["turn_off"] == []


async def test_a_short_reading_that_does_not_change_still_releases(hass, world):  # noqa: F811
    """The sensor is stepped: a shortfall that sits at one value sends one
    event. The release must not need a second."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 180)
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON
        supply.house = 1300
        supply.publish()
        await hass.async_block_till_done()
        # Nothing reports again. Only the clock moves.
        for _ in range(7):
            frozen.tick(timedelta(seconds=10))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON


async def test_a_reload_while_heating_on_the_surplus_keeps_it_counted(hass, world):  # noqa: F811
    """A reload forgets who was admitted. The heater found heating on the
    surplus is counted again on sight, so a shortfall still has someone to
    release - and one heater, not both."""
    with freeze_time(T0) as frozen:
        upstairs, downstairs = await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 180)
        assert relay(hass, DOWNSTAIRS_SWITCH) == STATE_ON

        for entry in (upstairs, downstairs):
            assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        world["turn_on"].clear()
        world["turn_off"].clear()
        await run(hass, frozen, supply, 30)
        assert world["turn_off"] == [] and world["turn_on"] == []

        supply.house = 1300
        await run(hass, frozen, supply, 100)
        assert len(world["turn_off"]) == 1
        assert [relay(hass, UPSTAIRS_SWITCH), relay(hass, DOWNSTAIRS_SWITCH)].count(
            STATE_ON
        ) == 1


async def test_a_reload_onto_a_reading_that_is_already_short_is_answered(hass, world):  # noqa: F811
    """Found in review. A reload sends no event, and a stepped sensor sitting at
    one short value sends none either: both heaters ran on for five minutes
    with nobody counted. The heater that started last still goes first."""
    with freeze_time(T0) as frozen:
        upstairs, downstairs = await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 180)
        supply.house = 1300
        supply.publish()
        await hass.async_block_till_done()
        for entry in (upstairs, downstairs):
            assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        world["turn_off"].clear()
        for _ in range(8):                                   # only the clock moves
            frozen.tick(timedelta(seconds=10))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        assert commanded(world["turn_off"]) == [UPSTAIRS_SWITCH]


async def test_a_restart_operates_no_relay_for_the_fit_until_the_grace_is_over(hass, world):  # noqa: F811
    """Both elements are found on at a start, on a surplus that is short. Inside
    the startup grace nothing is judged; after it one heater goes, not both."""
    with freeze_time(T0) as frozen:
        hass.states.async_set(UPSTAIRS_SWITCH, STATE_ON)
        hass.states.async_set(DOWNSTAIRS_SWITCH, STATE_ON)
        hass.states.async_set(PV_EXCESS, STATE_ON)
        hass.set_state(CoreState.starting)
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=1300)
        world["turn_on"].clear()
        world["turn_off"].clear()
        await run(hass, frozen, supply, 60)
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        await run(hass, frozen, supply, STARTUP_GRACE.total_seconds() - 10)
        assert world["turn_off"] == [] and world["turn_on"] == []
        # And nothing was decided in it that would bite when it ends: a wait
        # set inside the grace would park a tank that was heating before it.
        for entity_id in (UPSTAIRS, DOWNSTAIRS):
            assert hass.states.get(entity_id).attributes["surplus_boost_locked_until"] is None
        assert entities(hass)["Upstairs"]._fleet.surplus_snapshot()["claims"] == {}

        await run(hass, frozen, supply, 120)
        assert len(world["turn_off"]) == 1
        await run(hass, frozen, supply, 300)
        assert len(world["turn_off"]) == 1


async def test_the_reason_for_a_wait_survives_a_reload(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        upstairs, downstairs = await setup_pair(hass)
        supply = Supply(hass, pv=2800, house=800)
        await surplus_arrives(hass, frozen, supply, 200)
        assert label(hass, UPSTAIRS) == WAITING
        assert await hass.config_entries.async_reload(upstairs.entry_id)
        await hass.async_block_till_done()
        assert label(hass, UPSTAIRS) == WAITING
        assert hass.states.get(UPSTAIRS).attributes["surplus_boost_locked_until"]


async def test_the_surplus_leaving_ends_the_wait(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=2000, house=600, full=False)
        await surplus_arrives(hass, frozen, supply, 60)
        assert label(hass, UPSTAIRS) == WAITING
        hass.states.async_set(PV_EXCESS, STATE_OFF)
        await run(hass, frozen, supply, 120)
        assert label(hass, UPSTAIRS) == "Blocked by eco condition"
        assert hass.states.get(UPSTAIRS).attributes["fleet_hold_reason"] is None


# --- what counts as heat that depends on the surplus ---------------------------


async def test_with_no_policy_only_the_boosts_extra_heat_depends_on_the_surplus(hass, world):  # noqa: F811
    """Smart Eco off and a boost target of 65. A tank below its normal target
    heats whatever the surplus says and must never be made to wait for it; only
    the heat above what the thermostat would do is the surplus's."""
    with freeze_time(T0):
        await setup_pair(hass, **{CONF_SURPLUS_BOOST_TARGET: 65.0})
        heater = entities(hass)["Upstairs"]
        heater._smart_eco_mode = SMART_ECO_MODE_OFF
        heater._current_operation = STATE_ELECTRIC
        await heater.async_set_surplus_boost_enabled(True)
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()

        heater._current_temperature = 40.0          # below the normal 48
        assert not heater._heat_depends_on_surplus()
        heater._current_temperature = 55.0          # above it, heading for 65
        assert heater._heat_depends_on_surplus()


async def test_under_the_utility_only_the_boosts_extra_heat_depends_on_the_surplus(hass, world):  # noqa: F811
    """The policy is enforcing and the utility opens the gate as well. Heat up
    to the normal target would go on without the surplus; the boost's heat
    above it would not (it is what eco_gated calls boost-gated)."""
    with freeze_time(T0):
        await setup_pair(hass, source="Utility", **{CONF_SURPLUS_BOOST_TARGET: 65.0})
        heater = entities(hass)["Upstairs"]
        await heater.async_set_surplus_boost_enabled(True)
        hass.states.async_set(PV_EXCESS, STATE_ON)
        await hass.async_block_till_done()
        assert heater._is_smart_eco_enforcing()

        heater._current_temperature = 40.0
        assert not heater._heat_depends_on_surplus()
        heater._current_temperature = 55.0
        assert heater._heat_depends_on_surplus()


async def test_a_shed_heater_is_not_heating_on_the_surplus(hass, world):  # noqa: F811
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=3800, house=650)
        await surplus_arrives(hass, frozen, supply, 30)
        heater = entities(hass)["Upstairs"]
        assert heater._heat_depends_on_surplus()
        await hass.services.async_call(
            "generic_water_heater", "shed", {"entity_id": UPSTAIRS}, blocking=True
        )
        assert not heater._heat_depends_on_surplus()
        assert not heater._fleet.holds_surplus(heater._entry_id)


# --- the option ----------------------------------------------------------------


def test_a_power_sensor_needs_a_surplus_entity():
    assert _surplus_power_errors({CONF_SURPLUS_POWER_ENTITY: SPARE}) == {
        "base": "surplus_power_needs_surplus_entity"
    }
    assert (
        _surplus_power_errors(
            {CONF_SURPLUS_POWER_ENTITY: SPARE, CONF_SURPLUS_ENTITY: PV_EXCESS}
        )
        == {}
    )
    assert _surplus_power_errors({CONF_SURPLUS_ENTITY: PV_EXCESS}) == {}
    assert _surplus_power_errors({CONF_SURPLUS_POWER_ENTITY: "  "}) == {}


def test_a_cleared_power_sensor_is_stored_as_empty():
    """A cleared field does not come back from the form; absent, the old value
    would show through from entry.data."""
    assert _apply_cleared_and_defaults({})[CONF_SURPLUS_POWER_ENTITY] == ""


async def test_a_power_sensor_without_a_surplus_entity_is_ignored_at_setup(hass, world):  # noqa: F811
    """An entry edited by hand, or left over: the heater does not half-run the
    fit on it."""
    hass.states.async_set(SPARE, "0")
    entry = build_entry(
        "Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR,
        **{CONF_ECO_TEMPLATE: "", CONF_SURPLUS_POWER_ENTITY: SPARE},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entities(hass)["Upstairs"]._surplus_power_entity is None


async def test_performance_under_the_policy_is_still_heat_on_the_surplus(hass, world):  # noqa: F811
    """Choosing performance does not stand the eco policy down (the ON button
    does): the gate would still stop it when the surplus left, so the fit still
    sizes it. A disinfection cycle that waits for eco is the same heat."""
    with freeze_time(T0) as frozen:
        await setup_pair(hass)
        supply = Supply(hass, pv=500, house=600, full=False)
        await surplus_arrives(hass, frozen, supply, 150)
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": UPSTAIRS, "operation_mode": "performance"},
            blocking=True,
        )
        await run(hass, frozen, supply, 120)
        assert relay(hass, UPSTAIRS_SWITCH) == STATE_OFF
        assert label(hass, UPSTAIRS) == WAITING
