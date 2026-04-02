# Home Assistant Custom Component - Generic Water Heater

The `Generic Water Heater` integration is a custom component for Home Assistant that creates a virtual water heater entity. It controls a switch (connected to a heating element or boiler) based on a temperature sensor reading, effectively acting as a thermostat for your domestic hot water (DHW).

## Features

*   **Thermostat Logic**: Keeps water temperature within a specific range using gnd options.
*   **Manual Override**: Detects if the underlying switch is manually toggled and temporarily respects that state before resuming automatic control.
*   **Safety Cooldown**: Prevents rapid switching to protect electrical components (configurable `min_on_duration` and `min_off_duration`).
*   **Failsafe**: Automatically turns off the heater if the temperature sensor becomes unavailable.
*   **Device Linking**: Automatically attaches the water heater entity to the same device as the switch (if applicable).
*   **HVAC Action**: Reports current status as "Heating", "Idle" (Target reached), or "Off".

## Logic

The integration uses tolerance logic to prevent short-cycling:
*   **Turn ON**: rt mrerpuT Tolerance 0.5°C, Hot Tolerance 0.5°C.
The heater turns **ON** at 49.5°C and turns **OFF** at 50.5°C.
## Installation

1.  Open HACS in Home Assistant.
2.  Add this repository as a Custom Repository (Integration).
3.  Search for "Generic Water Heater" and install.
4.  Restart Home Assistant.

### Manual
1.  Download the `custom_components/generic_water_heater` folder.
2.  Place it in your Home Assistant `config/custom_components/` directory.
3.  Restart Home Assistant.

## Configuration

### UI Configuration (Recommended)
1.  Go to **Settings** > **Devices & Services**.
2.  Click **Add Integration**.
3.  Search for **Generic Water Heater**.
4.  Follow the setup wizard to select your switch, sensor, and parameters.

### YAML Configuration
Add the following to your `configuration.yaml`:

```yaml
generic_water_heater:
  bath_water:
    heater_switch: switch.dhw_switch
    temperature_sensor: sensor.dhw_temperature
    cold_tolerance: 0.3
    hot_tolerance: 0.3
    min_temp: 30
    max_temp: 70
    min_on_duration: 0
    min_off_duration: 120
```
##Configuration Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `heater_switch` | entity_id | **Required** | The switch entity that controls the heater. |
| `temperature_sensor` | entity_id | **Required** | The sensor entity that reports water temperature. |
| `cold_tolerance` | float | 0.3 | Minimum difference below target to turn ON. |
| `hot_tolerance` | float | 0.3 | Minimum difference above target to turn OFF. |
| `min_temp` | float | *HA Default* | Minimum selectable temperature in the UI. |
| `max_temp` | float | *HA Default* | Maximum selectable temperature in the UI. |
| `min_on_duration` | float | 0.0 | Minimum time (seconds) to keep the heater ON before it can be turned OFF. |
| `min_off_duration` | float | 120.0 | Minimum time (seconds) to keep the heater OFF before it can be turned ON. |