"""4.6.1: bucketed history, a deadline sensor, fewer writes, and the disinfection bypass bound."""

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.generic_water_heater import (
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    DOMAIN,
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
    legionella_risk_signal,
)
from custom_components.generic_water_heater import water_heater as water_heater_module
from custom_components.generic_water_heater.sensor import (
    HISTORY_BUCKET,
    _buckets_from_stored,
)
from tests.test_disinfection import (  # noqa: F401
    UPSTAIRS,
    choose,
    report_risk,
    setup_with_policy,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    TRIGGER,
    UPSTAIRS_SENSOR,
    auto_enable_custom_integrations,
    world,
)
from tests.test_legionella_entities import (
    DAYS_UNTIL,
    RISK,
    START,
    run_a_cycle,
    setup_entry,
)

DUE = "sensor.upstairs_disinfection_due"
MAX = "sensor.upstairs_highest_temperature_7_days"


async def reading(hass, frozen, minutes, value):
    frozen.move_to(START + timedelta(minutes=minutes))
    hass.states.async_set(UPSTAIRS_SENSOR, value)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Disinfection due: a deadline, and Due In without statistics
# ---------------------------------------------------------------------------


async def test_disinfection_due_is_the_deadline(hass, world):
    with freeze_time(START) as frozen:
        await setup_entry(hass)
        assert hass.states.get(DUE).state == "unknown"
        await run_a_cycle(hass, frozen)

    due = hass.states.get(DUE)
    assert due.attributes["device_class"] == "timestamp"
    assert "state_class" not in due.attributes
    completed = hass.states.get(RISK).attributes["last_disinfection_at"]
    from homeassistant.util import dt as dt_util

    assert dt_util.parse_datetime(due.state) == dt_util.parse_datetime(completed) + timedelta(days=10)


async def test_due_in_no_longer_keeps_statistics(hass, world):
    await setup_entry(hass)
    assert "state_class" not in hass.states.get(DAYS_UNTIL).attributes


# ---------------------------------------------------------------------------
# History in 5-minute buckets: fewer rows, smaller restore data
# ---------------------------------------------------------------------------


async def test_readings_inside_one_bucket_do_not_rewrite_the_history_sensors(hass, world):
    """A cooling tank used to rewrite both sensors on every reading."""
    with freeze_time(START) as frozen:
        await setup_entry(hass, **{CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR: True})
        await reading(hass, frozen, 0.5, "44.9")
        before = {entity: hass.states.get(entity) for entity in (MAX, RISK)}

        # Cooling, still inside the first 5-minute bucket.
        await reading(hass, frozen, 2, "44.8")
        await reading(hass, frozen, 4, "44.7")
        for entity, state in before.items():
            assert hass.states.get(entity).last_updated == state.last_updated, (
                f"{entity} was rewritten by a reading that changed nothing it shows"
            )

        # The next bucket is one more bucket, not one more reading.
        await reading(hass, frozen, 6, "44.6")
        assert hass.states.get(MAX).attributes["samples_tracked"] == (
            before[MAX].attributes["samples_tracked"] + 1
        )


async def test_a_cooling_reading_inside_the_open_bucket_does_not_move_the_figures(hass, world):
    """The bucket still filling is left out of the 7-day sums.

    Above 50 C the kill credit of an interval depends on its lowest reading,
    and while a tank cools every reading is a new low: counting the open bucket
    rewrote the risk sensor on every reading.

    Coverage note: this passes with the open bucket counted too, because the
    sums are cached until a bucket closes, so the cache alone stops the churn.
    What leaving the open bucket out buys is that the cached figures never
    carry a half-filled bucket's first reading; no test pins that.
    """
    with freeze_time(START) as frozen:
        await setup_entry(hass)
        # Climb in steps the spike filter accepts, then hold a closed 5-10 min
        # bucket at 61 C so its lowest reading is not what bounds the credit.
        for minute, value in ((1, "52.0"), (2, "58.0"), (3, "61.0")):
            await reading(hass, frozen, minute, value)
        for minute in (5.5, 7, 9):
            await reading(hass, frozen, minute, "61.0")
        await reading(hass, frozen, 10.5, "60.9")
        before = hass.states.get(RISK)
        await reading(hass, frozen, 12, "60.8")
        await reading(hass, frozen, 14, "60.7")
        assert hass.states.get(RISK).last_updated == before.last_updated


def test_history_stored_before_4_6_1_is_read_into_buckets():
    start = START
    raw = [
        {"timestamp": (start + timedelta(minutes=m)).isoformat(), "temperature": 40.0 + m}
        for m in range(12)
    ]
    buckets = _buckets_from_stored(raw)

    assert len(buckets) == 3
    assert [b.start for b in buckets] == [start + HISTORY_BUCKET * i for i in range(3)]
    assert (buckets[0].t_min, buckets[0].t_max) == (40.0, 44.0)
    assert buckets[0].max_at == start + timedelta(minutes=4)
    assert (buckets[2].t_min, buckets[2].t_max) == (50.0, 51.0)

    # And it round-trips in the new shape.
    again = _buckets_from_stored([b.as_stored() for b in buckets])
    assert again == buckets


async def test_the_risk_signal_fires_only_on_a_change(hass, world):
    """The heater re-evaluated its policy on every reading before 4.6.1."""
    with freeze_time(START) as frozen:
        await setup_entry(hass)
        entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
        fired = []
        async_dispatcher_connect(hass, legionella_risk_signal(entry_id), fired.append)

        for minute in range(1, 30, 3):
            await reading(hass, frozen, minute, f"{45 - minute / 10:.1f}")

    assert fired == [], "the risk signal fired for readings that changed no verdict"


# ---------------------------------------------------------------------------
# "Once" may stand eco down for 8 h, not the manual-pause bound
# ---------------------------------------------------------------------------


def smart_eco_mode(hass):
    return hass.states.get(UPSTAIRS).attributes["smart_eco_mode"]


async def test_a_once_cycle_keeps_its_bypass_past_the_pause_bound(hass, world):
    """The pause bound here is 6 h; a cold tank needs ~3 h plus the hour's hold."""
    with freeze_time(TRIGGER) as frozen, patch.object(
        water_heater_module.persistent_notification, "async_create"
    ):
        upstairs, _ = await setup_with_policy(hass)
        await report_risk(hass, upstairs, "Elevated")
        await choose(hass, "Once")
        await hass.async_block_till_done()
        assert smart_eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET
        assert hass.states.get(UPSTAIRS).attributes[
            "smart_eco_off_until_target_since"
        ].startswith(TRIGGER.isoformat()[:16]), "test premise: the bound starts now"

        frozen.move_to(TRIGGER + timedelta(hours=7))
        hass.states.async_set(UPSTAIRS_SENSOR, "41.0")  # near 40: not held by the spike filter
        await hass.async_block_till_done()
        assert smart_eco_mode(hass) == SMART_ECO_MODE_OFF_UNTIL_TARGET, (
            "the disinfection bypass ran out on the manual-pause bound"
        )

        frozen.move_to(TRIGGER + timedelta(hours=8, minutes=5))
        hass.states.async_set(UPSTAIRS_SENSOR, "41.5")
        await hass.async_block_till_done()
        assert smart_eco_mode(hass) != SMART_ECO_MODE_OFF_UNTIL_TARGET, (
            "the disinfection bypass is no longer bounded"
        )
