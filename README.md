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
- Fleet load coordination across every instance, so several heaters cannot step onto a shared inverter or generator at the same moment.
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
| `nominal_power_w` | number | `0` | Nameplate power of this heating element, in watts. Used for fleet load admission. `0` means unknown, which makes this heater invisible to the fleet power budget. |
| `fleet_stagger_seconds` | number | `60` | Minimum spacing between this heater switching on and any other instance switching on. `0` disables staggering. The largest value set on any instance applies to the whole fleet. |
| `fleet_power_budget_w` | number | `0` | Maximum combined nominal power all instances may have switched on at once. `0` disables the budget. The smallest non-zero value set on any instance applies to the whole fleet. |
| `smart_eco_manual_off_resume_hours` | number (slider) | `6` | Auto-resume/override duration in hours (range: `1` to `48`). Used by Auto Resume after Delay and Always ON temporary override countdowns. |
| `enable_max_temp_history_sensor` | boolean | `false` | Adds a sensor to the same device that exposes the highest recorded temperature in the last 7 days (useful in anti-legionella monitoring workflows). |

## Fleet Load Coordination

Several instances driven by one shared condition -- the same Smart Eco template, for example -- all
react in the same Home Assistant event-loop pass. Without coordination each one commands its switch
within milliseconds of its siblings, and a few kW of resistive load arrives as a single step. On an
inverter or generator sized close to the house load, that step is what trips the supply.

All instances share one coordinator, so they can see each other's commitments the instant they are
made. Three rules apply, in order, to every switch-on:

1. **Stagger** (`fleet_stagger_seconds`, default 60 s) -- no instance may switch on within N seconds
   of any other instance's switch-on.
2. **Nameplate budget** (`fleet_power_budget_w`, default off) -- a switch-on is refused if it would
   push the combined nominal power of all running instances over the budget.
3. **Priority** -- when capacity frees up, the heater furthest below its target temperature goes
   first. If a budget is set that cannot fit every heater at once, requests arriving in the same pass
   are held for one second so they are ranked by temperature deficit rather than by arrival order.

A refused heater is never skipped: it defers and retries on the same cooldown timer that
`min_off_duration` uses, and it is woken immediately when a sibling releases capacity.

Two deliberate design choices:

- **Nameplate, not telemetry.** Admission uses the configured `nominal_power_w`, not a power sensor.
  Power sensors typically poll every 15-60 s, far too slow to gate a decision that has to be made in
  the same event-loop pass that requested it. Nameplate accounting has zero latency. The trade-off is
  that `nominal_power_w` must be kept accurate by hand -- update it if an element is rewired or its
  power selector is moved.
- **The budget only prevents an aggregate step.** A heater is never blocked while no other instance
  is drawing, so a budget smaller than a single element cannot leave you with no hot water at all.

Manual switch-ons are counted against the budget too, since they draw real watts. The fleet can only
ever delay its **own** commands -- it never refuses a human. Flipping the physical switch works
exactly as before: the override is detected, Smart Eco steps back, and the watts are simply booked.

### Commitments are reconciled, not trusted

A commitment is booked the instant a switch-on is commanded, because waiting for the switch to
confirm would reintroduce the very latency this feature exists to avoid. That makes every commitment
a claim about the world, so it is checked against reality rather than trusted forever -- otherwise
one dead relay would quietly take the whole house's hot water with it:

- A commanded switch that has **not been seen on within 2 minutes** loses its claim, and a warning is
  logged naming the heater.
- A switch that has been **unavailable for 10 minutes** loses its claim. It is held at first, because
  it may still be drawing, but the likelier cause is that the element lost power.
- An **unloaded config entry** keeps its watts for 60 seconds, because unloading an entry does not
  switch its heater off. This covers the reload an options save triggers, which would otherwise be
  the exact moment both elements could come on together.
- A switch actually **observed on** never expires. It really is drawing.

Whenever a claim lapses, a waiting heater is woken immediately rather than left on its retry timer.

Set `fleet_stagger_seconds` to `0` and leave `fleet_power_budget_w` at `0` on every instance to
restore the previous uncoordinated behaviour.

Current fleet state is exposed on each water heater entity as the `nominal_power_w`,
`fleet_committed_power_w`, `fleet_power_budget_w`, `fleet_stagger_seconds` and `fleet_hold_reason`
attributes. `fleet_hold_reason` names exactly why a heater is currently waiting.

## Load shedding (external load balancer integration)

An external load balancer that protects a shared supply needs to drop this heater without its
request being mistaken for a person operating it. Calling `water_heater.set_operation_mode` for that
is indistinguishable from someone using the UI, so it trips the manual-override handling and pauses
Smart Eco for hours -- on the shed *and* again on the restore, leaving the tank heating outside its
eco condition.

Two entity services exist for that instead:

| Service | Effect |
| --- | --- |
| `generic_water_heater.shed` | Forces the heater off for load shedding. The operation mode and Smart Eco policy are left exactly as they are -- no pause, no countdown, no notification. |
| `generic_water_heater.release` | Releases the shed and resumes whatever was configured. No-op if not shed. |

Behaviour while shed:

- It outranks everything, including Smart Eco `Always ON`. Shedding protects the electrical supply;
  it is not a user preference. Smart Eco's "eco allows heating, restore the heating mode" path
  cannot undo a shed.
- The `min_on_duration` hold is bypassed. That minimum exists to stop thermostat noise
  short-cycling the relay, and letting it delay a supply-protection action would hand the balancer a
  shed that has silently not happened yet. A **release** does still respect `min_off_duration`, so
  the element may take up to that long to come back.
- A person asking for heat wins -- `turn_on`, setting a heating operation mode, or flipping the
  physical switch all clear the shed. A request to turn the heater *off* does not clear it; it has
  nothing to win, and clearing on an ignored OFF could switch the element back on.
- The heater reports `load_shed: true` and a Smart Eco state of `Shed by load balancer`.
- The shed releases the heater's share of the fleet power budget, so a sibling can use the capacity.

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
