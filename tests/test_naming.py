"""New helper entities take their entity_id from the heater's name, not the device's.

Home Assistant builds the entity_id of a has_entity_name entity from the
DEVICE name. Devices get renamed, so an entity added in a later release came
out unlike its siblings (seen with Disinfection Due in 4.6.1:
`sensor.<renamed device>_disinfection_due` beside `sensor.upstairs_...`).
"""

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.generic_water_heater import (
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_LEGIONELLA_INTERVAL_DAYS,
    DOMAIN,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)

OPTIONS = {CONF_ENABLE_LEGIONELLA_SENSOR: True, CONF_LEGIONELLA_INTERVAL_DAYS: 10}


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_renamed_device_does_not_name_new_entities(hass, world):
    """The device is renamed after setup; a sensor enabled later must not follow it."""
    hass.states.async_set(UPSTAIRS_SENSOR, "45.0")
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR)
    await setup(hass, entry)
    assert hass.states.get("sensor.upstairs_disinfection_due") is None, "test premise"

    devices = dr.async_get(hass)
    device = next(iter(dr.async_entries_for_config_entry(devices, entry.entry_id)))
    devices.async_update_device(device.id, name_by_user="Relay Box")

    # Turning the Legionella sensor on adds its entities on the reload.
    hass.config_entries.async_update_entry(entry, options={**entry.data, **OPTIONS})
    await hass.async_block_till_done()

    assert hass.states.get("sensor.upstairs_disinfection_due") is not None
    assert hass.states.get("sensor.upstairs_legionella_risk") is not None
    assert hass.states.get("sensor.relay_box_disinfection_due") is None


async def test_an_existing_entity_keeps_its_id(hass, world):
    hass.states.async_set(UPSTAIRS_SENSOR, "45.0")
    entry = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR, **OPTIONS)
    registry = er.async_get(hass)
    entry.add_to_hass(hass)
    existing = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_legionella_disinfection_due",
        config_entry=entry,
        suggested_object_id="relay_box_disinfection_due",
    )
    assert existing.entity_id == "sensor.relay_box_disinfection_due"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.relay_box_disinfection_due") is not None
    assert hass.states.get("sensor.upstairs_disinfection_due") is None
