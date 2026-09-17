"""The config form is grouped into sections; storage stays flat.

Sections are presentation only. The form submits nested dicts, and every
consumer in this integration reads flat keys off entry.data / entry.options, so
the flattening between the two is load-bearing: get it wrong and an options save
silently drops whatever it failed to unwrap.
"""

import json
import pathlib

import pytest
import voluptuous as vol

from homeassistant.const import CONF_NAME

from custom_components.generic_water_heater import (
    CONF_COLD_TOLERANCE,
    CONF_ECO_TEMPLATE,
    CONF_ENABLE_HOT_WATER_IN_USE,
    CONF_ENABLE_LEGIONELLA_SENSOR,
    CONF_HEATER,
    CONF_SENSOR,
    CONF_WATER_IN_USE_ENTITY,
)
from custom_components.generic_water_heater.config_flow import (
    SECTIONS,
    _apply_cleared_and_defaults,
    _build_data_schema,
    flatten_sections,
)

TRANSLATIONS = (
    pathlib.Path(__file__).parent.parent
    / "custom_components/generic_water_heater/translations/en.json"
)


def test_every_sectioned_field_survives_flattening():
    """The whole risk in one test: nothing may be lost on the way in."""
    submitted = {
        CONF_NAME: "Upstairs",
        CONF_HEATER: "switch.element",
        CONF_SENSOR: "sensor.tank",
        **{name: {field: f"value-of-{field}" for field in fields}
           for name, fields in SECTIONS.items()},
    }

    flat = flatten_sections(submitted)

    for name, fields in SECTIONS.items():
        assert name not in flat, f"section wrapper {name} leaked into storage"
        for field in fields:
            assert flat[field] == f"value-of-{field}", f"{field} was lost"
    assert flat[CONF_NAME] == "Upstairs"


def test_a_missing_section_does_not_blank_its_fields():
    """An absent section means "no news", not "clear everything inside it"."""
    flat = flatten_sections({CONF_NAME: "Upstairs", "temperatures": {CONF_COLD_TOLERANCE: 2}})

    assert flat[CONF_COLD_TOLERANCE] == 2
    assert "min_on_duration" not in flat


def test_every_schema_field_is_accounted_for_in_a_section_or_at_top_level():
    """A field added to the schema but not to SECTIONS would never be stored."""
    schema = _build_data_schema()
    top_level = {str(k.schema) for k in schema.schema}
    sectioned = {f for fields in SECTIONS.values() for f in fields}

    for key in schema.schema:
        name = str(key.schema)
        if name in SECTIONS:
            inner = schema.schema[key].schema.schema
            for inner_key in inner:
                assert str(inner_key.schema) in sectioned, (
                    f"{inner_key.schema} is in the form but not in SECTIONS, "
                    "so flatten_sections would not know to unwrap it"
                )
        else:
            assert name in {CONF_NAME, CONF_HEATER, CONF_SENSOR}, (
                f"{name} sits at the top level; either section it or allow it here"
            )
    assert not (sectioned & top_level), "a field is both top-level and sectioned"


def test_cleared_optional_fields_are_persisted_as_empty():
    """Cleared must mean cleared: entry.data is merged UNDER entry.options."""
    flat = _apply_cleared_and_defaults(
        {CONF_NAME: "Upstairs", "smart_eco": {}, "hot_water_in_use": {}}
    )

    assert flat[CONF_ECO_TEMPLATE] == ""
    assert flat[CONF_WATER_IN_USE_ENTITY] == ""
    assert flat[CONF_ENABLE_HOT_WATER_IN_USE] is False
    assert flat[CONF_ENABLE_LEGIONELLA_SENSOR] is False


def test_an_existing_flat_entry_round_trips_unchanged():
    """Editing an entry and saving without touching anything must change nothing."""
    current = {
        CONF_NAME: "Upstairs",
        CONF_HEATER: "switch.element",
        CONF_SENSOR: "sensor.tank",
        CONF_COLD_TOLERANCE: 2.0,
        CONF_ECO_TEMPLATE: "{{ true }}",
        CONF_ENABLE_LEGIONELLA_SENSOR: True,
        CONF_WATER_IN_USE_ENTITY: "binary_sensor.flow",
        CONF_ENABLE_HOT_WATER_IN_USE: True,
    }
    schema = _build_data_schema(current)

    # Simulate the form handing back each section populated from its defaults.
    submitted = {CONF_NAME: current[CONF_NAME], CONF_HEATER: current[CONF_HEATER],
                 CONF_SENSOR: current[CONF_SENSOR]}
    for key in schema.schema:
        name = str(key.schema)
        if name not in SECTIONS:
            continue
        inner = schema.schema[key].schema.schema
        populated = {}
        for k in inner:
            default = getattr(k, "default", None)
            if default is None or not callable(default):
                continue
            value = default()
            if value is not vol.UNDEFINED:
                populated[str(k.schema)] = value
        submitted[name] = populated

    flat = _apply_cleared_and_defaults(submitted)

    assert flat[CONF_COLD_TOLERANCE] == 2.0
    assert flat[CONF_ENABLE_LEGIONELLA_SENSOR] is True
    assert flat[CONF_ENABLE_HOT_WATER_IN_USE] is True


# ---------------------------------------------------------------------------
# Translations must describe the same shape the schema builds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("area", ["config", "options"])
def test_translations_cover_every_section_and_field(area):
    """A section with no translation renders as a raw key in the UI."""
    data = json.loads(TRANSLATIONS.read_text())
    steps = data[area]["step"]
    step = next(iter(steps.values()))

    assert set(step["sections"]) == set(SECTIONS), f"{area}: section list differs"
    for name, fields in SECTIONS.items():
        block = step["sections"][name]
        assert block.get("name"), f"{area}.{name} has no title"
        for field in fields:
            assert field in block["data"], f"{area}.{name}.{field} has no label"


def test_the_water_in_use_label_no_longer_claims_to_create_the_sensor():
    """It used to gate creation. It does not any more, and the label said so."""
    text = TRANSLATIONS.read_text()
    assert "creates a Hot Water In Use sensor" not in text
