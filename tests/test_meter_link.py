"""The heater's meter under its device's "Linked devices".

These need Home Assistant 2026.9+, where each integration has its own device
and devices sharing an identifier are linked; on older cores the heater and
switch share ONE device and an identifier cannot be on two devices at all.

Home Assistant links devices that share an identifier. The heater's device
already carries its switch device's identifiers; when the meter is a separate
device (a relay fed by a separate metering breaker), the meter device's
identifiers are declared too, so clearing the meter unlinks it on the next
reload by itself. 4.3.x's "Connected via" link is cleared only if unchanged.
"""

import pytest
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_water_heater import DOMAIN, LINKED_DEVICES_SINCE
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

NEW_CORE = (MAJOR_VERSION, MINOR_VERSION) >= LINKED_DEVICES_SINCE
linked = pytest.mark.skipif(
    not NEW_CORE, reason="devices sharing identifiers need Home Assistant 2026.9+"
)

METER = "sensor.element_meter"
RELAY_ID = ("other", "relay")
METER_ID = ("other", "meter")


def _devices(hass):
    """A relay device carrying the switch, a meter device carrying the meter."""
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    dreg, ereg = dr.async_get(hass), er.async_get(hass)
    relay = dreg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={RELAY_ID}, name="Relay"
    )
    meter = dreg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={METER_ID}, name="Meter"
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


def _heater_device(hass):
    entity = er.async_get(hass).async_get("water_heater.upstairs")
    return dr.async_get(hass).async_get(entity.device_id)


@linked
async def test_a_separate_meter_device_is_linked(hass, world):
    _devices(hass)
    await _setup(hass, power_sensor=METER)
    dev = _heater_device(hass)
    assert METER_ID in dev.identifiers and RELAY_ID in dev.identifiers
    assert dev.via_device_id is None, "a linked device, not Connected via"
    switch_device_id = er.async_get(hass).async_get(UPSTAIRS_SWITCH).device_id
    assert switch_device_id != dev.id, "2026.9: the heater has its own device"
    assert METER_ID not in dr.async_get(hass).async_get(switch_device_id).identifiers, (
        "the switch's own device is not touched"
    )


async def test_a_meter_on_the_switch_device_adds_nothing(hass, world):
    """The downstairs shape: the switch IS the meter."""
    _, relay, _ = _devices(hass)
    er.async_get(hass).async_update_entity(METER, device_id=relay.id)
    await _setup(hass, power_sensor=METER)
    assert METER_ID not in _heater_device(hass).identifiers


async def test_no_meter_adds_nothing(hass, world):
    _devices(hass)
    await _setup(hass)
    assert METER_ID not in _heater_device(hass).identifiers


@linked
async def test_clearing_the_meter_removes_only_what_we_added(hass, world):
    _devices(hass)
    entry = await _setup(hass, power_sensor=METER)
    await _reconfigure(hass, entry, power_sensor="")
    dev = _heater_device(hass)
    assert METER_ID not in dev.identifiers
    assert RELAY_ID in dev.identifiers, "the switch's identifier is never taken"


@linked
async def test_the_link_survives_a_reload_and_follows_a_new_meter(hass, world):
    other, _, _ = _devices(hass)
    entry = await _setup(hass, power_sensor=METER)
    await _reconfigure(hass, entry)
    assert METER_ID in _heater_device(hass).identifiers
    dreg, ereg = dr.async_get(hass), er.async_get(hass)
    meter2 = dreg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "meter2")}, name="M2"
    )
    ereg.async_get_or_create(
        "sensor", "other", "meter2-w", device_id=meter2.id,
        suggested_object_id="element_meter_2",
    )
    await _reconfigure(hass, entry, power_sensor="sensor.element_meter_2")
    dev = _heater_device(hass)
    assert ("other", "meter2") in dev.identifiers
    assert METER_ID not in dev.identifiers


async def test_the_4_3_connected_via_link_is_cleared(hass, world):
    """4.3.x set via_device and recorded it; 4.4 clears it if unchanged."""
    _, relay, meter = _devices(hass)
    dr.async_get(hass).async_update_device(relay.id, via_device_id=meter.id)
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, power_sensor=METER)
    await Store(hass, 1, f"{DOMAIN}.meter_links").async_save(
        {entry.entry_id: {"device": relay.id, "via": meter.id}}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(relay.id).via_device_id is None


async def test_a_connected_via_set_by_someone_else_stays(hass, world):
    """A via link not recorded as ours (the downstairs heater has one) stays."""
    other, relay, _ = _devices(hass)
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "hub")}, name="Hub"
    )
    dr.async_get(hass).async_update_device(relay.id, via_device_id=hub.id)
    await _setup(hass, power_sensor=METER)
    assert dr.async_get(hass).async_get(relay.id).via_device_id == hub.id


@pytest.mark.skipif(NEW_CORE, reason="the guard for cores before 2026.9")
async def test_an_older_core_declares_nothing_and_still_sets_up(hass, world):
    """Before 2026.9 an identifier on two devices is a collision that would fail
    the heater's setup. On those cores nothing is declared."""
    _devices(hass)
    await _setup(hass, power_sensor=METER)
    assert hass.states.get("water_heater.upstairs") is not None
    assert METER_ID not in _heater_device(hass).identifiers


async def test_a_4_3_link_changed_since_is_left_alone(hass, world):
    """The store says we linked relay -> meter, but someone has since pointed
    the relay at a hub: that is no longer ours to clear."""
    other, relay, meter = _devices(hass)
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "hub")}, name="Hub"
    )
    dr.async_get(hass).async_update_device(relay.id, via_device_id=hub.id)
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, power_sensor=METER)
    await Store(hass, 1, f"{DOMAIN}.meter_links").async_save(
        {entry.entry_id: {"device": relay.id, "via": meter.id}}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(relay.id).via_device_id == hub.id
    assert await Store(hass, 1, f"{DOMAIN}.meter_links").async_load() == {}
