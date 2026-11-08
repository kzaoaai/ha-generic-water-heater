"""Config entries older than version 4 migrate without losing what they were created with."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_water_heater import (
    CONF_DEBUG_LOGGING,
    CONF_ECO_TEMPLATE,
    CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
    CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
    DOMAIN,
)
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    PV_EXCESS,
    UPSTAIRS_SENSOR,
    UPSTAIRS_SWITCH,
    auto_enable_custom_integrations,
    build_entry,
    world,
)


def v3_entry(options=None):
    current = build_entry("Upstairs", UPSTAIRS_SWITCH, UPSTAIRS_SENSOR)
    data = {k: v for k, v in current.data.items() if k != CONF_ECO_TEMPLATE}
    data.update(
        {
            "eco_entity": PV_EXCESS,
            "eco_value": "on",
            CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR: True,
            CONF_DEBUG_LOGGING: True,
        }
    )
    return MockConfigEntry(
        domain=DOMAIN, title="Upstairs", version=3, data=data, options=options or {}
    )


async def test_a_v3_entry_migrates_its_eco_entity_to_a_template(hass, world):
    entry = v3_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 4
    assert entry.data[CONF_ECO_TEMPLATE] == "{{ states('%s') == 'on' }}" % PV_EXCESS
    assert "eco_entity" not in entry.data and "eco_value" not in entry.data


async def test_migration_does_not_shadow_data_with_option_defaults(hass, world):
    """Options win over data in every reader; a default there overrode creation-time choices."""
    entry = v3_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for key in (
        CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR,
        CONF_DEBUG_LOGGING,
        CONF_SMART_ECO_MANUAL_OFF_RESUME_HOURS,
    ):
        assert key not in entry.options, f"migration wrote a default for {key} into options"
    assert entry.data[CONF_ENABLE_MAX_TEMP_HISTORY_SENSOR] is True
    assert entry.data[CONF_DEBUG_LOGGING] is True
    assert hass.states.get("sensor.upstairs_highest_temperature_7_days") is not None


async def test_legacy_eco_keys_in_options_are_still_converted(hass, world):
    entry = v3_entry(options={"eco_entity": PV_EXCESS, "eco_value": "on"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.options[CONF_ECO_TEMPLATE] == "{{ states('%s') == 'on' }}" % PV_EXCESS
    assert "eco_entity" not in entry.options
