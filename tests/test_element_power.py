"""The Power (mirror) sensor: the meter's reading on the heater's own device.

For the UI only. The guarantees are about NOT being counted twice: no
state_class (so no statistics and nothing the Energy dashboard can take),
`mirror_of` naming the meter, never a mirror of a mirror.
"""

import pytest
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.generic_water_heater import DOMAIN
from custom_components.generic_water_heater.config_flow import _power_sensor_errors
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

METER = "sensor.element_meter"
MIRROR = "sensor.upstairs_power_mirror"


async def _setup(hass, **extra):
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, **extra)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _meter(hass, value, unit="W"):
    hass.states.async_set(
        METER, value, {"unit_of_measurement": unit, "device_class": "power"}
    )


async def test_the_mirror_follows_the_meter(hass, world):
    _meter(hass, "1371.4")
    await _setup(hass, power_sensor=METER)
    state = hass.states.get(MIRROR)
    assert float(state.state) == pytest.approx(1371.4)
    assert state.attributes["unit_of_measurement"] == "W"
    assert state.attributes["mirror_of"] == METER

    _meter(hass, "0.0")
    await hass.async_block_till_done()
    assert float(hass.states.get(MIRROR).state) == 0.0


async def test_it_can_never_feed_statistics_or_the_energy_dashboard(hass, world):
    """The double-count guard: the meter carries the statistics, the mirror none."""
    _meter(hass, "1000")
    await _setup(hass, power_sensor=METER)
    assert "state_class" not in hass.states.get(MIRROR).attributes


@pytest.mark.parametrize("bad", [STATE_UNAVAILABLE, "unknown", "garbage"])
async def test_any_doubt_reads_unavailable(hass, world, bad):
    _meter(hass, "1000")
    await _setup(hass, power_sensor=METER)
    _meter(hass, bad)
    await hass.async_block_till_done()
    assert hass.states.get(MIRROR).state == STATE_UNAVAILABLE


async def test_a_missing_meter_reads_unavailable(hass, world):
    await _setup(hass, power_sensor=METER)
    assert hass.states.get(MIRROR).state == STATE_UNAVAILABLE


async def test_no_meter_no_mirror(hass, world):
    await _setup(hass)
    assert hass.states.get(MIRROR) is None


async def test_a_mirror_is_never_mirrored(hass, world):
    """An entry naming one of this integration's own entities as its meter (the
    flow refuses that; this covers one written another way) gets no mirror."""
    _meter(hass, "1000")
    first = await _setup(hass, power_sensor=METER)
    assert hass.states.get(MIRROR) is not None
    hass.config_entries.async_update_entry(
        first, options={**first.data, "power_sensor": MIRROR}
    )
    await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    # Not re-created: Home Assistant leaves the registry entry's placeholder,
    # marked restored, instead of a live entity mirroring itself.
    state = hass.states.get(MIRROR)
    assert state is None or state.attributes.get("restored") is True


async def test_the_flow_refuses_a_mirror_as_the_meter(hass, world):
    _meter(hass, "1000")
    await _setup(hass, power_sensor=METER)
    assert _power_sensor_errors(hass, {"power_sensor": MIRROR}) == {
        "base": "power_sensor_is_mirror"
    }
    assert _power_sensor_errors(hass, {"power_sensor": METER}) == {}
    assert _power_sensor_errors(hass, {"power_sensor": ""}) == {}


async def test_the_options_form_refuses_a_mirror_and_keeps_the_entry(hass, world):
    """End to end through the real options flow: the save is refused with the
    error and the stored meter is untouched."""
    from homeassistant.data_entry_flow import FlowResultType

    _meter(hass, "1000")
    entry = await _setup(hass, power_sensor=METER)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        "name": "Upstairs",
        "heater_switch": UPSTAIRS_SWITCH,
        "temperature_sensor": UPSTAIRS_SENSOR,
        "temperatures": {},
        "cycle_protection": {},
        "smart_eco": {},
        "fleet": {},
        "legionella": {},
        "hot_water_in_use": {},
        "metering": {"power_sensor": MIRROR},
        "advanced": {},
    }
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "power_sensor_is_mirror"}
    assert {**entry.data, **entry.options}["power_sensor"] == METER


async def test_the_mirror_never_matches_a_power_suffix(hass, world):
    """A template summing every sensor whose id ends `_power` (one exists,
    dormant, on the author's install) must not pick up the copy."""
    _meter(hass, "1000")
    await _setup(hass, power_sensor=METER)
    ids = [s.entity_id for s in hass.states.async_all("sensor")
           if s.attributes.get("mirror_of")]
    assert ids and not any(i.endswith("_power") for i in ids)


def test_both_naming_paths_end_in_mirror():
    """Production heaters' devices carry a name, so Home Assistant composes
    "<device> Power (mirror)"; a nameless device gets it spelled out. Either way
    the slug must end `_power_mirror`."""
    from homeassistant.util import slugify

    from custom_components.generic_water_heater.sensor import ElementPowerSensor

    named = ElementPowerSensor(None, "e", "Upstairs", METER, {("x", "y")}, True)
    bare = ElementPowerSensor(None, "e", "Upstairs", METER, None, False)
    assert named.has_entity_name and slugify(f"Upstairs Water Heater {named.name}").endswith("_power_mirror")
    assert not bare.has_entity_name and slugify(bare.name).endswith("_power_mirror")
