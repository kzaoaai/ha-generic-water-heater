"""What a restart must bring back, including when the last saved state is unusable."""

from datetime import timedelta
import json

from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.helpers.json import JSONEncoder
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.generic_water_heater import (
    SMART_ECO_MODE_OFF_UNTIL_TARGET,
    SMART_ECO_MODE_UNTIL_MANUAL,
)
from custom_components.generic_water_heater.water_heater import HeaterStoredData
from tests.test_integration_setup import (  # noqa: F401  (fixtures)
    auto_enable_custom_integrations,
    entities,
    setup_both,
    world,
)

UPSTAIRS = "water_heater.upstairs"


async def test_a_restart_saved_mid_dropout_restores_from_the_stored_copy(hass, world):
    """The published state was `unavailable`: its attributes carry nothing.

    Home Assistant saves restore data every 15 minutes and at stop, and the
    heater publishes `unavailable` whenever its switch is off the network. A
    restart caught there used to come back OFF with every policy setting lost.
    """
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(UPSTAIRS, STATE_UNAVAILABLE, {}),
                {
                    "state": STATE_OFF,
                    "attributes": {
                        "temperature": 52,
                        "smart_eco_mode": SMART_ECO_MODE_UNTIL_MANUAL,
                        "smart_eco_pause_reason": "until_manual",
                    },
                },
            ),
        ),
    )
    await setup_both(hass)
    upstairs = entities(hass)["Upstairs"]

    assert upstairs.target_temperature == 52
    assert upstairs._smart_eco_mode == SMART_ECO_MODE_UNTIL_MANUAL
    assert upstairs._smart_eco_pause_reason == "until_manual"


async def test_a_usable_published_state_still_wins(hass, world):
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(UPSTAIRS, STATE_OFF, {"temperature": 48}),
                {"state": STATE_OFF, "attributes": {"temperature": 52}},
            ),
        ),
    )
    await setup_both(hass)
    assert entities(hass)["Upstairs"].target_temperature == 48


async def test_the_stored_copy_round_trips_through_json(hass, world):
    await setup_both(hass)
    stored = entities(hass)["Upstairs"].extra_restore_state_data
    data = json.loads(json.dumps(stored.as_dict(), cls=JSONEncoder))
    back = HeaterStoredData.from_dict(data)
    assert back is not None
    assert back.state == stored.state
    assert back.attributes["temperature"] == stored.attributes["temperature"]
    assert "smart_eco_mode" in back.attributes


async def test_the_disinfection_bypass_flag_survives_a_restart(hass, world):
    """Not published, so only the stored copy can carry it."""
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    UPSTAIRS,
                    STATE_OFF,
                    {"temperature": 60, "smart_eco_mode": SMART_ECO_MODE_OFF_UNTIL_TARGET},
                ),
                {
                    "state": STATE_OFF,
                    "attributes": {"temperature": 60},
                    "off_until_target_for_disinfection": True,
                },
            ),
        ),
    )
    await setup_both(hass)
    assert entities(hass)["Upstairs"]._off_until_target_for_disinfection is True


async def test_a_boost_lockout_survives_a_restart(hass, world):
    until = dt_util.utcnow() + timedelta(minutes=20)
    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                "electric",
                {"temperature": 60, "surplus_boost_locked_until": until.isoformat()},
            ),
        ),
    )
    await setup_both(hass)
    upstairs = entities(hass)["Upstairs"]
    assert upstairs._surplus_boost_locked()

    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert not upstairs._surplus_boost_locked()
    assert upstairs._surplus_boost_lockout_timer is None


async def test_an_expired_boost_lockout_is_not_restored(hass, world):
    mock_restore_cache(
        hass,
        (
            State(
                UPSTAIRS,
                "electric",
                {
                    "temperature": 60,
                    "surplus_boost_locked_until": (
                        dt_util.utcnow() - timedelta(minutes=1)
                    ).isoformat(),
                },
            ),
        ),
    )
    await setup_both(hass)
    upstairs = entities(hass)["Upstairs"]
    assert not upstairs._surplus_boost_locked()
    assert upstairs._surplus_boost_lockout_timer is None
