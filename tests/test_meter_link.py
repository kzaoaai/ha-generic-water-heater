""""Connected via": the heater's device links to its meter's device.

Only when the meter is a different device from the heater switch (a relay fed
by a separate metering breaker). Only a link this integration made is ever
replaced or cleared - a hub link the switch's own integration set stays.
"""

from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

METER = "sensor.element_meter"


def _devices(hass):
    """A relay device carrying the switch, a meter device carrying the meter."""
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    dreg, ereg = dr.async_get(hass), er.async_get(hass)
    relay = dreg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "relay")}, name="Relay"
    )
    meter = dreg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "meter")}, name="Meter"
    )
    # The world fixture already set a state on the switch id; free it, or the
    # registry would hand out "..._2".
    hass.states.async_remove(UPSTAIRS_SWITCH)
    sw = ereg.async_get_or_create(
        "switch", "other", "relay-sw", device_id=relay.id,
        suggested_object_id=UPSTAIRS_SWITCH.split(".")[1],
    )
    assert sw.entity_id == UPSTAIRS_SWITCH
    hass.states.async_set(UPSTAIRS_SWITCH, "off")
    ereg.async_get_or_create(
        "sensor", "other", "meter-w", device_id=meter.id,
        suggested_object_id=METER.split(".")[1],
    )
    assert ereg.async_get(METER).device_id == meter.id
    hass.states.async_set(METER, "0", {"unit_of_measurement": "W"})
    return other, relay, meter


async def _setup(hass, **extra):
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, **extra)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _reconfigure(hass, entry, **changes):
    hass.config_entries.async_update_entry(entry, options={**entry.data, **changes})
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


def _via(hass, device):
    return dr.async_get(hass).async_get(device.id).via_device_id


async def test_a_separate_meter_device_is_linked(hass, world):
    _, relay, meter = _devices(hass)
    await _setup(hass, power_sensor=METER)
    assert _via(hass, relay) == meter.id


async def test_a_meter_on_the_switch_device_is_not_linked(hass, world):
    """The downstairs shape: the switch IS the meter."""
    _, relay, _ = _devices(hass)
    er.async_get(hass).async_update_entity(METER, device_id=relay.id)
    await _setup(hass, power_sensor=METER)
    assert _via(hass, relay) is None


async def test_no_meter_no_link(hass, world):
    _, relay, _ = _devices(hass)
    await _setup(hass)
    assert _via(hass, relay) is None


async def test_clearing_the_meter_removes_our_link(hass, world):
    _, relay, meter = _devices(hass)
    entry = await _setup(hass, power_sensor=METER)
    assert _via(hass, relay) == meter.id
    await _reconfigure(hass, entry, power_sensor="")
    assert _via(hass, relay) is None


async def test_a_hub_link_set_by_the_switch_integration_is_left_alone(hass, world):
    other, relay, _ = _devices(hass)
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "hub")}, name="Hub"
    )
    dr.async_get(hass).async_update_device(relay.id, via_device_id=hub.id)
    entry = await _setup(hass, power_sensor=METER)
    assert _via(hass, relay) == hub.id, "never overwrite a link we did not make"
    await _reconfigure(hass, entry, power_sensor="")
    assert _via(hass, relay) == hub.id, "nor clear one"


async def test_a_link_changed_by_someone_else_is_not_cleared(hass, world):
    other, relay, meter = _devices(hass)
    entry = await _setup(hass, power_sensor=METER)
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "hub")}, name="Hub"
    )
    dr.async_get(hass).async_update_device(relay.id, via_device_id=hub.id)
    await _reconfigure(hass, entry, power_sensor="")
    assert _via(hass, relay) == hub.id


async def test_the_link_survives_a_reload_and_follows_a_new_meter(hass, world):
    other, relay, meter = _devices(hass)
    entry = await _setup(hass, power_sensor=METER)
    await _reconfigure(hass, entry)
    assert _via(hass, relay) == meter.id
    dreg, ereg = dr.async_get(hass), er.async_get(hass)
    meter2 = dreg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "meter2")}, name="M2"
    )
    ereg.async_get_or_create(
        "sensor", "other", "meter2-w", device_id=meter2.id,
        suggested_object_id="element_meter_2",
    )
    await _reconfigure(hass, entry, power_sensor="sensor.element_meter_2")
    assert _via(hass, relay) == meter2.id
