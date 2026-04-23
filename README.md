# Home Assistant Custom Component - Generic Water Heater

The `Generic Water Heater` integration creates a virtual water heater entity in Home Assistant. It controls a switch or input boolean using a temperature sensor, so you can manage domestic hot water with water heater controls in the UI and in automations.

## Features

- Thermostat-style control with configurable cold and hot tolerances.
- Optional Eco mode, controlled by a Home Assistant template condition.
- `turn_on` maps to `electric` mode.
- Optional mapping of `turn_off` to `eco` (disabled by default).
- Explicit `off` remains available in the operation mode selector.
- Optional extra sensor that tracks the highest recorded temperature in the last 7 days.
- Manual override handling when the underlying switch is toggled directly.
- Minimum on and off durations to avoid rapid switching.
- Failsafe shutdown when the temperature sensor becomes unavailable.
- Automatic device linking to the same device as the controlled switch when possible.

## Heating Logic

The integration uses hysteresis to avoid short-cycling:

- Heat turns on when the current temperature is less than or equal to `target_temperature - cold_tolerance`.
- Heat turns off when the current temperature is greater than or equal to `target_temperature + hot_tolerance`.

Example with target `50°C`, cold tolerance `0.5°C`, and hot tolerance `0.5°C`:

- Heater turns on at `49.5°C` or lower.
- Heater turns off at `50.5°C` or higher.

In `performance` mode the heater stays on continuously. In `eco` mode the heater only runs when the Eco template condition evaluates to true.

## Installation

1. Open HACS in Home Assistant.
2. Add this repository as a Custom Repository for Integrations.
3. Search for `Generic Water Heater` and install it.
4. Restart Home Assistant.

## Configuration

This integration is configured from the Home Assistant UI.

1. Go to **Settings** > **Devices & Services**.
2. Click **Add Integration**.
3. Search for **Generic Water Heater**.
4. Select the heater switch, temperature sensor, and your preferred operating parameters.

## Configuration Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `heater_switch` | entity_id | Required | The switch or input boolean that controls the heater. |
| `temperature_sensor` | entity_id | Required | The sensor that reports the water temperature. |
| `target_temperature_step` | float | `1.0` | The step used by the target temperature control in the UI. |
| `cold_tolerance` | float | `0.0` | Difference below target temperature that allows heating to turn on. |
| `hot_tolerance` | float | `0.0` | Difference above target temperature that forces heating to turn off. |
| `min_temp` | float | `15.0` | Minimum selectable target temperature. |
| `max_temp` | float | `80.0` | Maximum selectable target temperature. |
| `min_on_duration` | duration | `0 seconds` | Minimum time the heater must stay on before it can be turned off. |
| `min_off_duration` | duration | `120 seconds` | Minimum time the heater must stay off before it can be turned on. |
| `eco_mode_template_condition` | template | empty | Eco mode is disabled by default when this template is empty. When defined, Eco mode becomes available and this boolean template decides if or when Eco mode is allowed to heat. Useful for users who only want to heat using solar PV power or during specific times of day. |
| `map_turn_off_to_eco` | boolean | `false` | When enabled, `turn_off` maps to `eco` while `off` remains selectable in the operation mode dropdown. Useful for HomeKit workflows where normal off actions should prefer eco unless `electric` is explicitly selected. |
| `enable_max_temp_history_sensor` | boolean | `false` | Adds a sensor to the same device that exposes the highest recorded temperature in the last 7 days. |

## Eco Mode

When `eco_mode_template_condition` is configured, `eco` becomes an available operation mode.

Examples:

```jinja
{{ is_state('binary_sensor.solar_surplus', 'on') }}
```

```jinja
{{ states('sensor.grid_price_level') in ['low', 'very_low'] }}
```

```jinja
{{ is_state('input_boolean.allow_eco_heating', 'on') }}
```

If the Eco template evaluates to false, the heater stays off even if the water temperature is below the target.