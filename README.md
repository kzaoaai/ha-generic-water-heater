# Home Assistant Custom Component - Generic Water Heater

The `Generic Water Heater` integration creates a virtual water heater entity in Home Assistant. It controls a switch using a temperature sensor, so you can manage domestic hot water with water heater controls in the UI and in automations.

## Features

- Thermostat-style control with configurable cold and hot tolerances.
- Smart Eco policy controlled by a dedicated select entity (Smart Eco Mode) plus a template condition.
- Smart Eco State sensor that exposes meaningful policy states (Off, Idle, Heating in eco, Blocked by eco condition, countdown states, and override states), including two bypasses that put the policy back by themselves once the tank is satisfied.
- Optional extra sensor that tracks the highest recorded temperature in the last 7 days, useful for legionella prevention workflows.
- Manual override handling for both water heater entity actions and direct underlying switch toggles.
  A switch returning from `unavailable` is **not** treated as a manual action: it is a device
  reconnecting and reporting the state it already had. Without that distinction a brief network
  dropout could be read as "someone flipped it on", promoting `electric` to `performance` and
  running the element to the tank's own mechanical cutout. A genuine flip still arrives as a real
  `off` -> `on` transition and behaves exactly as before.
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

The config flow groups these into collapsible sections — temperatures, cycle protection, Smart
Eco, fleet, legionella, hot water in use, advanced — with the name, switch and sensor left at the
top. The grouping is presentation only: options are stored flat under the keys below, so YAML,
diagnostics and anything reading the entry see no sections.

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
| `min_off_duration` | duration | `120 seconds` | Minimum time the heater must stay off before it can be turned on. A configured `0` means zero; the default applies only when the option is absent. |
| `eco_mode_template_condition` | template | empty | Boolean template used by Smart Eco policy. If empty, Smart Eco Mode entities are not created and no Smart Eco policy is applied. |
| `enable_legionella_sensor` | boolean | `false` | Adds the Legionella thermal-conditions sensor described below. |
| `legionella_interval_days` | number | `7` | How long a completed disinfection cycle counts for before the sensor reports Elevated (and twice that before High). |
| `nominal_power_w` | number | `0` | Nameplate power of this heating element, in watts. Used for fleet load admission. `0` means unknown, which makes this heater invisible to the fleet power budget. |
| `fleet_stagger_seconds` | number | `60` | Minimum spacing between this heater switching on and any other instance switching on. `0` disables staggering. The largest value set on any instance applies to the whole fleet. |
| `fleet_power_budget_w` | number | `0` | Maximum combined nominal power all instances may have switched on at once. `0` disables the budget. The smallest non-zero value set on any instance applies to the whole fleet. |
| `smart_eco_manual_off_resume_hours` | number (slider) | `6` | Auto-resume/override duration in hours (range: `1` to `48`). Used by Auto Resume after Delay and Always ON temporary override countdowns. |
| `enable_max_temp_history_sensor` | boolean | `false` | Adds a sensor to the same device that exposes the highest recorded temperature in the last 7 days (useful in anti-legionella monitoring workflows). |
| `enable_hot_water_in_use` | boolean | `false` | Adds the Hot Water In Use binary sensor described below. |
| `water_in_use_entity` | entity_id | empty | Optional whole-house flow or pump signal. **Corroborates** the Hot Water In Use sensor on ambiguous fall rates; it does not gate it, and leaving it empty only costs the middle tier. |

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
- `Off until target reached`: stands the policy down, then puts back whichever option was in force before, by itself, once the tank is satisfied. Described below.
- `On until next manual control`: policy stops when manual control is detected.
- `Auto Resume after Delay`: manual control pauses policy and resumes automatically after the configured delay.
- `Always ON`: policy is enforced continuously for normal entity-level manual actions. Manual changes on the underlying switch create a temporary timed override, then enforcement resumes automatically.

High-level behavior:

- If Smart Eco policy is actively enforcing and template is false, heating is blocked.
- If Smart Eco policy is actively enforcing and template is true, heating is allowed.
- If water heater mode is `off` while policy allows heating, last heating mode is restored.

### Heat now, without leaving the policy off

Under `Auto Resume after Delay` there is a **one-shot bypass**, and it is the plain **ON** button
(`water_heater.turn_on`) rather than anything in the Smart Eco Mode select. It behaves differently
from every other manual action: instead of the timed pause, Smart Eco pauses with **no deadline**,
the tank heats on `electric` regardless of the template, and the policy **resumes by itself** once
the tank has reached target and stayed idle for a minute. The Smart Eco State sensor reads
`Paused until the tank is satisfied` while that is in effect.

Use it when you want hot water now and do not want to remember to switch the policy back on. Turning
the tank **off**, or changing the operation mode, takes the *timed* path instead
(`smart_eco_manual_off_resume_hours`), which reads as a `Resuming in HH MM` countdown.

**This does not work for `performance`.** That mode holds the element on unconditionally, so the
appliance's own mechanical thermostat is what eventually stops it and Home Assistant never sees
that — `hvac_action` stays `heating`, the tank never reads idle, and the pause would never resolve.
For a deliberate high-temperature run, use `Off until target reached` below, which is bounded on
time as well and so cannot hang on a tank that never reads idle.

### `Off until target reached`

The same idea reached from the select instead of the ON button, and available under any policy
option rather than only `Auto Resume after Delay`. Choose it when you want hot water now regardless
of the eco condition, and do not want to remember to switch the policy back on.

- **It reverts to the option that was in force before**, not to a default. Re-selecting it while it
  is already active does not overwrite that memory.
- **It is bounded.** Past `smart_eco_manual_off_resume_hours` the policy comes back regardless, with
  a persistent notification saying the tank never got there. The bound is the point: a dead element
  or a target the tank cannot reach must not be able to leave Smart Eco disabled indefinitely, which
  is the failure this option exists to avoid.
- **"Target reached" means idle with no disinfection cycle still outstanding**, not simply idle.
  `performance` never reports idle, so on that path the time bound is what ends it.
- **It un-parks a tank Smart Eco had already switched off.** Eco parks the operation mode at `off`
  and only its own restore branch lifts that, so without this the option would leave the tank
  sitting off — the opposite of what it is for.
- The Smart Eco State sensor reads `Off until the tank reaches target` while it is in effect.

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

## Hot Water In Use

Optional per-tank sensor, created when you enable **Hot Water In Use** in the config flow. The
water-in-use entity is corroboration rather than a prerequisite, so creation does not hang on it.

It answers a question a whole-house flow or pump signal cannot: whether hot water is being drawn
**from this tank**. That signal fires for a cold tap, an irrigation valve, or another appliance
entirely, and it cannot tell you which. What identifies one tank is its own temperature falling
faster than standing loss can manage.

Two tiers, because measured draws do not all separate cleanly from cooling on rate alone:

| Fall rate | Verdict |
| --- | --- |
| ≥ 0.8 °C/min | A draw. Roughly 6× the fastest cooling ever measured on these tanks, so nothing else reaches it. Reported on the tank's evidence alone. |
| 0.35 – 0.8 °C/min | A draw **only if** the configured water-in-use entity agrees. Real, but close enough to post-cutout cooling to be arguable on its own. |
| < 0.35 °C/min | Not a draw. |

The external entity is therefore **corroboration, not a gate**. It can break a tie; it can never veto
an unambiguous signal from the tank. That matters in two real cases: a draw served from a pressure
tank without engaging the pump still registers, and so does one during an outage of the pump sensor.
The attribute `water_in_use_agrees` records what it had to say — `true`, `false`, or `null` when it
could not say anything.

Calibrated against six hand-verified draws on a real tank, which averaged 0.365 to 2.53 °C/min, and
against the fastest passive cooling on record, 0.13 °C/min in the minutes just after a thermostat
cutout. The regression tests replay those traces.

**What it deliberately does not do:**

- **No special case for the element being on.** A draw steep enough to matter outruns the roughly
  0.15 °C/min the element adds, so one rule catches the mid-heat cases too — several of the verified
  draws were mid-heat. The cost is that a draw which merely *cancels* heating goes undetected. The
  alternative, treating a flat trace as a draw, rested on a single observation out of fifteen.
- **Short draws are missed.** A fall has to last at least 30 seconds and total at least 0.8 °C. A
  hand wash will not register; a shower will.
- **It cannot distinguish a hot draw from anything else that cools this tank fast.** Nothing else
  plausibly does, but that is an argument from the absence of a mechanism, not a measurement.

Attributes expose the rate, the observed drop, when the draw started and both thresholds, so a
disagreement can be argued with rather than guessed at.

## Legionella Risk Sensor

An optional sensor reporting how favourable this tank's recent temperature history has been to
Legionella growth, and how long since it last reached a disinfection temperature. Enable it with
`enable_legionella_sensor`.

**Read this before relying on it.** It is a *thermal-conditions index computed from one sensor at
one height*. It is not a measurement of contamination, and only a laboratory culture or PCR test can
tell you what is actually in your water. It is structurally blind to:

- **The coldest water in the tank.** In a 151 L electric storage tank with the thermostat at 66 °C
  the measured base was still 43.2 °C. Electric tanks are heated by side-wall immersion elements, so
  water below the lowest element moves only by weak convection.
- **Sediment**, where thermal disinfection fails worst — 50 °C for 4 hours produced no measurable
  inactivation in water-heater deposits, and 55 °C left culturable cells after 24 hours.
- **Biofilm and amoebae**, which shelter Legionella through sub-60 °C cycles.
- **Every outlet downstream.** Distal pipework cools to room temperature within ~25 minutes of a
  draw regardless of tank setpoint.

### What it reports

State is `Low`, `Elevated`, `High`, or `Unknown`, driven by how long it has been since a qualifying
cycle relative to `legionella_interval_days` (`Elevated` past one interval, `High` past two). The
numbers behind it are attributes: `days_since_disinfection`, `hold_progress_minutes`,
`hours_in_growth_band_7d`, `equivalent_log10_reduction_7d` and `max_temperature_7d`.

`hold_progress_minutes` is live, so a manual high-temperature session can be watched as it
accumulates.

### The model

- A **qualifying cycle is 60 °C held for one continuous hour** at the sensor. Both figures are fixed,
  not configurable. Guidance that prescribes a cycle at all (HSE HSG274 Part 2 cl. 2.25/2.28, ESGLI
  cl. 3.154) asks for the whole vessel at ≥60 °C for an hour, and **below 60 °C a cycle is not a
  gentler version of the same thing** — after a 4 h/55 °C shock with amoebae present, populations
  rebounded 5 log₁₀ *higher* than controls within four days. A sub-60 °C option would be a footgun.
- **The hold is hysteretic.** A mechanical tank thermostat cycles — slow decay, fast re-heat — so a
  bare threshold is the wrong tool. A real 200 L tank rippled 59.6–64 °C with dips *below* 60 °C
  lasting 19 and 28 minutes while sitting at pasteurisation temperature for five hours; a strict
  "continuous hour ≥60 °C" would have reported it as never disinfected. So the window **opens** at
  60 °C, stays open while the tank holds above 59 °C, and closes for good below that. Only time
  genuinely at or above 60 °C counts toward the hour — ripple keeps the window open, it does not earn
  credit. Sitting at 59.5 °C forever never opens a hold at all.
- **An abandoned hold is discarded, never banked.** Partial treatment is the failure mode this is
  meant to detect, not something to award partial credit for. It takes **two consecutive** readings
  below 59 °C to abandon one, though: a single implausible sample — one bad packet from a networked
  sensor reading one point on a stratified tank — must not throw away a nearly complete hour. That
  grace is deliberately narrow. A reading more than a degree below the sustain threshold, or a
  recovery that arrives outside the normal sampling cadence, abandons the hold immediately, and the
  forgiven interval earns no credit either way.
- **A hold in flight survives a restart.** Most of a real hold is banked with the element already
  off, coasting down from the thermostat cutout — a measured 200 L tank credited its final minutes
  seven minutes *after* its switch turned off — so a cycle routinely spans an evening. Progress is
  persisted and resumed, but only when the gap across the restart is inside the 30-minute observation
  limit. Longer than that and there is no evidence the tank stayed hot while Home Assistant was down,
  so the hold is discarded rather than resumed.
- **Growth band is 20–50 °C**, deliberately wider than the 20–45 °C regulatory trigger: measured
  multiplication does not stop until 48.4–50.0 °C, so a tank plateauing at 47 °C is still growing.
- **Disinfection credit** uses the published inactivation kinetics (D₅₅ = 3.47 min, z = 5.54 °C),
  accrued only within the model's validated 51–61 °C range and clamped at the top of it so nothing
  above 61 °C is extrapolated.
- **Every interval is credited at the lower of its two endpoint temperatures**, so a brief spike
  between two widely spaced samples cannot claim the whole gap. Gaps longer than 30 minutes are
  treated as unobserved rather than as held temperature.

### Running a disinfection cycle

With the risk sensor enabled, each tank also gets a **Legionella Disinfection** select:

| Option | Behaviour |
| --- | --- |
| `Off` | Inert. The default, and what a cycle returns to when it completes. |
| `Disinfect` | Runs **one** cycle, then clears itself back to `Off`. Nothing ever starts again without you asking. |
| `Disinfect ASAP` | The same one-shot, but it does not wait for the eco condition: starting a cycle also sets Smart Eco to `Off until target reached`, which stands the policy down and gives it back by itself. |
| `Always ON` | Standing policy. Runs again every time the risk sensor reports the interval has lapsed. |

Choosing a policy while the risk reads `Elevated` or `High` puts the tank into `performance` and
leaves it there until the sensor reports `Low`, then hands it back to the mode it had. The cycle is
**goal-seeking, not timed** — it is a standing request for temperature, not a fixed run.

It deliberately **does not outrank anything**:

- **Smart Eco still gates it — unless you asked for ASAP.** The eco condition decides whether the
  tank actually heats; the cycle only asks. Because the request is also written to `smart_eco_last_heating_mode`, a cycle survives
  the nightly gap — the eco gate parks the mode at `off` and restores `performance` next time the
  condition returns, so a cycle can span several days without anyone re-arming it.
- **A load shed still drops it.** Shedding protects the supply and is checked first, so a balancer
  can take the tank down mid-cycle with no special-casing. The request stays standing.
- **A tank you switched off stays off — unless you ask for a cycle now.** `Disinfect` and
  `Disinfect ASAP` are commands and will start on an `off` tank; `Always ON` is a standing policy
  and will not, because a policy should not overrule a mode you chose. This distinction carries
  the weight once Smart Eco is itself `Off`, since there is then no way to tell an eco-parked
  `off` from a deliberate one. A cycle
  started from `off` returns the tank to `off`, not to `electric` — otherwise, with no eco gate left,
  it would quietly hold its target on grid for ever after a cycle you thought was one-shot.

**Taking the tank back ends the cycle.** Changing the operation mode yourself — or turning the tank
off, at the wall or in the UI — stops the cycle and sets the policy to `Off`, so a standing
`Always ON` cannot pull the tank straight back into `performance` the moment the risk sensor next
reports. Re-arming is one tap. Asking for `performance` yourself does *not* end a cycle, since that is what it
already wants.

If you want it to finish *faster*, choose `Disinfect ASAP` rather than pausing Smart Eco by hand.
It does the same thing — stands the policy down — but through `Off until target reached`, so the
policy is restored for you instead of waiting to be remembered. On a tank whose element cannot
reach 60 °C inside one eco window, standing the policy down is the only way a cycle will ever
complete.

**It gives up after three days** and tells you, via a persistent notification, how far the hold
actually got. That bound is on the calendar rather than on run length on purpose: most of a hold is
banked with the element already off, coasting down, so a run-length cap would abort precisely the
part that earns the credit. A tank that has not got there in three days is not going to without
help.

### Reaching a disinfection temperature

`performance` mode heats continuously, ignoring the target, until the appliance's own mechanical
thermostat opens — so it needs no change to `max_temp`. Two things to know:

- **The mechanical thermostat is the real ceiling.** If it is set below 60 °C no cycle can ever
  qualify, and the attempt would be exactly the sub-60 °C treatment described above.
- **Smart Eco can cut a session short.** Switching from `electric` to `performance` is not a heating
  boundary change, so it does not pause Smart Eco — and when the eco condition goes false the heater
  is forced off mid-session. Switching from `off` to `performance` *does* pause Smart Eco, which
  gives an uninterrupted session. Set the heater to `off` first, or set Smart Eco to
  `Off until target reached` for the duration.

## Acknowledgments

This project was originally inspired by the upstream work from [@dgomes](https://github.com/dgomes) on Generic Water Heater.
Thanks for the original implementation and idea that this variant builds on.
