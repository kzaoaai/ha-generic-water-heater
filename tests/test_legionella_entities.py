"""Days Until Disinfection and Disinfection Hold Progress, as entities.

Until 3.0.0 both were attributes of the Legionella Risk sensor
(`days_since_disinfection`, `hold_progress_minutes`), which gave them no
history and needed a template to drive anything.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.generic_water_heater import (
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_LEGIONELLA_INTERVAL_DAYS,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

RISK = "sensor.upstairs_legionella_risk"
DAYS_UNTIL = "sensor.upstairs_days_until_disinfection"
PROGRESS = "sensor.upstairs_disinfection_hold_progress"
START = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


async def setup_entry(hass, enabled=True):
    hass.states.async_set(UPSTAIRS_SENSOR, "45.0")
    entry = build_entry(
        "Upstairs",
        UPSTAIRS_SWITCH,
        UPSTAIRS_SENSOR,
        **{CONF_ENABLE_LEGIONELLA_SENSOR: enabled, CONF_LEGIONELLA_INTERVAL_DAYS: 10},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def run_a_cycle(hass, frozen):
    """Hold the sensor above 60 C for just over the hour, in 1-minute steps."""
    for i in range(63):
        hass.states.async_set(UPSTAIRS_SENSOR, f"{61.0 + (i % 2) * 0.1:.1f}")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=1))


async def test_both_exist_only_with_the_risk_sensor(hass, world):  # noqa: F811
    await setup_entry(hass, enabled=False)
    assert hass.states.get(DAYS_UNTIL) is None
    assert hass.states.get(PROGRESS) is None


async def test_the_old_attributes_are_gone(hass, world):  # noqa: F811
    await setup_entry(hass)
    attrs = hass.states.get(RISK).attributes
    assert "days_since_disinfection" not in attrs
    assert "hold_progress_minutes" not in attrs


async def test_days_until_is_unknown_before_any_cycle(hass, world):  # noqa: F811
    await setup_entry(hass)
    assert hass.states.get(DAYS_UNTIL).state == "unknown"
    assert hass.states.get(DAYS_UNTIL).attributes["unit_of_measurement"] == "d"


async def test_progress_counts_up_through_a_hold_and_resets_on_completion(hass, world):  # noqa: F811
    with freeze_time(START) as frozen:
        await setup_entry(hass)
        assert float(hass.states.get(PROGRESS).state) == 0.0
        for i in range(31):
            hass.states.async_set(UPSTAIRS_SENSOR, f"{61.0 + (i % 2) * 0.1:.1f}")
            await hass.async_block_till_done()
            frozen.tick(timedelta(minutes=1))
        assert float(hass.states.get(PROGRESS).state) == pytest.approx(29.0, abs=1.1)

        # 29 + 63 minutes: the hour completes, banks a cycle, resets to zero,
        # and a fresh hold opens on the next sample still above 60 C.
        await run_a_cycle(hass, frozen)
        assert hass.states.get(DAYS_UNTIL).state != "unknown"
        assert float(hass.states.get(PROGRESS).state) < 35.0


async def test_days_until_counts_down_from_the_interval_and_goes_negative(hass, world):  # noqa: F811
    with freeze_time(START) as frozen:
        await setup_entry(hass)
        await run_a_cycle(hass, frozen)
        assert float(hass.states.get(DAYS_UNTIL).state) == pytest.approx(10.0, abs=0.05)

        # No temperature report at all: the clock alone has to move it.
        frozen.tick(timedelta(days=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert float(hass.states.get(DAYS_UNTIL).state) == pytest.approx(-1.0, abs=0.05)
