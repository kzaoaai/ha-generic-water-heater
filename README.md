# Home Assistant Custom Component - Generic Water Heater

The `Generic Water Heater` integration creates a virtual water heater entity in Home Assistant. It controls a switch using a temperature sensor, so you can manage domestic hot water with water heater controls in the UI and in automations.

## Features

- Thermostat-style control with configurable cold and hot tolerances.
- Smart Eco policy controlled by a dedicated select entity (Smart Eco Mode) plus a template condition.
- Smart Eco State sensor that exposes meaningful policy states (Off, Idle, Heating in eco, Blocked by eco condition, countdown states, and override states).
- Optional extra sensor that tracks the highest recorded temperature in the last 7 days, useful for legionella prevention workflows.
- Manual override handling for both water heater entity actions and direct underlying switch toggles.
- Always ON temporary override behavior for manual underlying switch changes, with countdown state and persistent notifications.
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

Operation behavior:

- `off`: heater stays off.
- `electric`: follows the threshold logic above.
- `performance` (Boost): prioritizes heating.
- Smart Eco Mode: applies policy behavior described below.

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
| `heater_switch` | entity_id | Required | The switch entity that controls the heater. |
| `temperature_sensor` | entity_id | Required | The sensor that reports the water temperature. |
| `target_temperature_step` | float | `1.0` | The step used by the target temperature control in the UI. |
| `cold_tolerance` | float | `0.0` | Difference below target temperature that allows heating to turn on. |
| `hot_tolerance` | float | `0.0` | Difference above target temperature that forces heating to turn off. |
| `min_temp` | float | `15.0` | Minimum selectable target temperature. |
| `max_temp` | float | `80.0` | Maximum selectable target temperature. |
| `min_on_duration` | duration | `0 seconds` | Minimum time the heater must stay on before it can be turned off. |
| `min_off_duration` | duration | `120 seconds` | Minimum time the heater must stay off before it can be turned on. |
| `eco_mode_template_condition` | template | empty | Boolean template used by Smart Eco policy. If empty, Smart Eco Mode entities are not created and no Smart Eco policy is applied. |
| `smart_eco_manual_off_resume_hours` | number (slider) | `6` | Auto-resume/override duration in hours (range: `1` to `48`). Used by Auto Resume after Delay and Always ON temporary override countdowns. |
| `enable_max_temp_history_sensor` | boolean | `false` | Adds a sensor to the same device that exposes the highest recorded temperature in the last 7 days (useful in anti-legionella monitoring workflows). |

## Smart Eco Mode

Smart Eco Mode is a policy layer, not a water heater operation mode.

When an eco template is configured, the integration exposes:

- Select: `Smart Eco Mode`
- Sensor: `Smart Eco State`

Available Smart Eco Mode options:

- `Off`: no Smart Eco policy enforcement.
- `On until next manual control`: policy stops when manual control is detected.
- `Auto Resume after Delay`: manual control pauses policy and resumes automatically after the configured delay.
- `Always ON`: policy is enforced continuously for normal entity-level manual actions. Manual changes on the underlying switch create a temporary timed override, then enforcement resumes automatically.

High-level behavior:

- If Smart Eco policy is actively enforcing and template is false, heating is blocked.
- If Smart Eco policy is actively enforcing and template is true, heating is allowed.
- If water heater mode is `off` while policy allows heating, last heating mode is restored.

Always ON temporary override details:

- Trigger: manual toggle of the underlying heater switch (for example, panel/smart-breaker action).
- Duration: uses `smart_eco_manual_off_resume_hours`.
- State sensor: shows `Always ON override (Resuming in XXH YYM)`.
- Notifications: Home Assistant persistent notifications are created when override starts and when policy resumes.

Examples:

```jinja
{{ is_state('binary_sensor.solar_surplus', 'on') }}
```

```jinja
{{ states('sensor.grid_price_level') in ['low', 'very_low'] }}
```

```jinja
{{ states('sensor.pv_generation_w') | float(0) > 3000 }}
```

```jinja
{{ is_state('input_boolean.allow_eco_heating', 'on') }}
```

If Smart Eco policy is active and the template evaluates to false, heating is blocked even if the target would otherwise request heat.

## Acknowledgments

This project was originally inspired by the upstream work from [@dgomes](https://github.com/dgomes) on Generic Water Heater.
Thanks for the original implementation and idea that this variant builds on.
