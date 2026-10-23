# Working on this integration

A HACS custom integration: a virtual `water_heater` entity that drives a switch from a temperature
sensor, plus a Smart Eco policy layer, legionella risk tracking and disinfection, a hot-water draw
detector, and switch-on staggering across instances.

**This repository is public.** Nothing that identifies a private network belongs in it — no
hostnames, LAN IPs, MAC addresses, SSIDs, tunnel or account IDs, and no personal names, including
where they are baked into entity IDs. Use the documentation-reserved ranges and neutral names in
tests, fixtures, issues and commit messages. Test fixtures are where this leaks, because realistic
names read better. Sanitize before the first push: a force-push does not take a leak back, it only
makes it look handled.

Deployment specifics, which instance is which, and what is currently live are **operator context and
deliberately not recorded here**. Ask, or read the operator's own notes.

---

## Ground rules that cost real money to rediscover

Each of these has already broken something. They are not style preferences.

### Never add a fourth entry to `operation_list`

HomeKit's `set_heat_cool` walks the list **in order**; Alexa and Google expose the raw mode
strings; an external load balancer snapshots the raw `state`. The list is
`[electric, off, performance]` — that order is part of the contract, not incidental. Three modes and
four consumers, none of which are exercised by this test suite.

### A `select` option rename is a breaking change you cannot absorb in code

`SelectEntity.async_handle_select_option` is `@final` and calls `_valid_option_or_raise` **before**
the entity's own `async_select_option`. So a caller passing an old option string gets a
`ServiceValidationError` at the component layer and your entity never sees it — a legacy map inside
the entity only helps the `RestoreEntity` path. **Before renaming any option, grep every consumer:
automations, scripts, scenes, dashboards, other integrations, node-red flows.** A renamed option
turns an automation into a silent no-op.

The legacy map is still required, separately, for restore: a select restores from its own last state
*string*, so without it an upgrade quietly reads the default.

The Legionella select and the water heater both restore the policy, and since 2026.x the platforms
set up in either order. The heater's own history wins; `runtime["legionella_restored"]` (absent
until the heater has looked, `False` if it found nothing) tells the select whether to restore its
own and hand it to the heater with `adopt_restored_legionella_mode`, which is not a person choosing
and so starts no one-shot. Tests that care force each order by patching
`async_forward_entry_setups`; left unpinned, they flake.

### The options flow is sectioned, but storage is FLAT — and a partial submission is destructive

The form groups fields into collapsible sections; `flatten_sections()` keeps what is stored flat, so
YAML, diagnostics and anything reading the entry see no sections. Keep it that way.

`_apply_cleared_and_defaults()` `setdefault`s **every** optional key, because a field cleared in the
UI simply does not come back from the form. Two fields — the eco template and the water-in-use
entity — have **no schema default, only a `description.suggested_value`**. So anything that builds
an options submission from schema defaults drops them, and the flow then blanks them. Blanking the
eco template disables the Smart Eco policy outright.

**If you write options programmatically, do not trust a tool's "patch semantics" claim.** Read the
entry, submit every current value explicitly in the sectioned shape, and diff the options afterwards.

### Smart Eco parks the operation mode at OFF, and only its own restore branch lifts that

Any new code path that stands the eco policy down must also *un-park*, or it leaves the tank sitting
off — the opposite of what standing eco down is for. And the un-park must be **scoped and one-shot**:
whether an `off` came from the eco gate or from a person has to be decided **at the moment the mode
changes**, because one line later the gate is no longer enforcing and the two are
indistinguishable. An unscoped un-park energises a tank its owner switched off, and reverses every
later `turn_off` for the duration — making `off` unreachable.

Related trap: `set_operation_mode` does not touch `smart_eco_last_heating_mode` on the OFF path, so
whatever is in there — possibly `performance` — is what a restore resumes. An unbounded
`performance` run is this integration's worst failure mode and has happened twice.

### A load shed is not a satisfied tank

`hvac_action` reports `off` only when the *operation mode* is off. A shed deliberately leaves the
mode alone and forces only the switch, so it reads **`idle`** — identical to a tank that reached
target. Any "has it finished?" test must exclude `load_shed` explicitly.

Shedding outranks everything, including the eco policy's Always ON. It protects the supply; it is
not a user preference.

### `performance` never reports idle

It holds the element on unconditionally and the appliance's mechanical thermostat is what stops it,
which Home Assistant never sees. Any mechanism that waits for `hvac_action == "idle"` will wait
forever on that path and needs a time bound as well.

### Power balancing is not this integration's job (since 2.0.0)

`fleet.py` does one thing: **stagger** switch-on commands so two elements cannot step onto a shared
inverter in the same event-loop pass. Nameplate-watt admission was removed in 2.0.0 — a dedicated
load balancer owns that, and two systems balancing one supply from two sets of nameplates disagree
silently. The supported seam is the `shed` / `release` services.

Three invariants in `fleet.py` look like leftovers of the removed budget and are not. The 2.0.0
first pass deleted them and an adversarial review caught it:

1. **`believed_on` makes re-asking idempotent.** A member believed to be drawing is admitted without
   re-anchoring. Without that, a heater whose switch stops reporting while the element is still
   closed re-asks on every control pass, walks its own anchor forward, and starves its sibling for
   as long as the reporting gap lasts.
2. **Observed switch-ON opens the window.** An ON this integration did not command — a person at the
   wall, another automation, a switch found on after a restart — energises the same inverter.
3. **The anchor survives a reload.** A reload is `unregister` **then** `register`, so state kept on
   the member object is lost. It is stashed outside the member. An options save is precisely when two
   elements could otherwise come on together.

### Every consumer of the tank sensor goes through the spike filter

`temperature_tracking.async_track_filtered_temperature` is the only way to subscribe to the
temperature sensor. A single reading >10 °C from the last accepted one is held until the next
reading decides it (or 60 s pass). Combined relay/temperature devices emit one wild reading as the
relay switches; before the filter, one such reading discarded a near-complete disinfection hold.
`unavailable`/`unknown` are never delayed — the failsafe acts on them. A new consumer that
subscribes with `async_track_state_change_event` directly reintroduces the bug.

### A Home Assistant start is not the eco condition going false

For `STARTUP_GRACE` (2 min) after `homeassistant_started` — not after entity load — a FALSE eco
condition neither parks nor restores; the restored mode stands. A TRUE one is trusted at once.
Switch flips in that window are not manual intent. Only a real start gets the window: a
config-entry reload has its sources loaded. Without this, every restart switched heating tanks off
because the template's source entities did not exist yet.

### The integration's own switch traffic is never a person

Manual-override detection in `_async_switch_changed` skips, in order: the startup window; stale
events (a later state already landed); our own commands reporting back (`_commands_in_flight`,
consumed within `OWN_ECHO_WINDOW`, 30 s — then control re-asserts intent); a switch returning from
unavailable. The echo record **must expire**: without it, a lost OFF followed by a wall OFF later
reads as our echo and the element comes back on, making OFF unreachable. Two genuine flips 50 ms
apart still count as a person — nothing distinguishes a fast replay from a hand.

The stale-event branch was written against 2025.1, which delivered state listeners with
`call_soon`, so two back-to-back writes produced an overtaken event. 2026.x runs them
synchronously inside `async_set`, so real state writes no longer reach that branch; the test
fires the stale `state_changed` event on the bus directly. The branch stays as a guard, not as
something production exercises.

### A disinfection cycle ends on a completed hold, not on the risk verdict

One-shots (`Once`, `Once (eco)`) start immediately even when risk is Low; the standing `As needed`
options wait for Elevated, and after a 3-day give-up they stay armed but back off one interval
(`disinfection_retry_after`). Internal mode values keep their pre-4.0.0 spellings because they are
stored state; only the select labels changed. So completion is "a hold completed after `disinfection_started_at`"
(`_cycle_completed_since_start`); reading "risk is Low" would end a cycle started on a Low tank the
moment it began.

### `power_sensor` and `eco_gated` are a contract with another integration

`power_sensor` is the configured meter on the element's circuit (or `None`), published only,
never read for control. `eco_gated` is `True` only while the heat depends on the Smart Eco
condition — it would stop if the condition went false (`_eco_gated`: policy enforcing, the
condition true and the startup grace over, `electric`, no disinfection, not shed). A battery runtime estimate in another integration
leaves an `eco_gated` heater's metered draw out of what the battery must carry when the
gating supply leaves. So a false `True` hides real load: **any new way of heating regardless
of the eco condition must make `_eco_gated` false.** Boost-only heat is the one addition on the
other side: with the tank past its normal target plus hot tolerance, the boost's heat stops when
surplus leaves, which is when a battery would have to carry it, so it reads true even with the
policy off or paused. Inside the normal hysteresis band it reads false, because the element would
stay on regardless. Renaming or removing either attribute
is a breaking change (bump major); publishing `""` instead of `None` for an unset meter would
break it too.

### The eco condition has named inputs; the template is only an extra term

Since 4.5.0 the eco condition is: the surplus entity active, OR the utility entity active and
the **Eco: Use Utility** switch on, OR the template true. Each entity matches its own active
state, ignoring case (an enum power-source sensor reads `Utility`, not `on`). The template stays
as an optional OR term on purpose: an entry that is never reconfigured keeps working, and removing
it would drop that entry to "no eco condition", where Smart Eco is inert and the tank heats on any
source, including a battery overnight (`_is_smart_eco_enforcing` needs `_has_eco_condition()`).
Do not reuse the legacy `eco_entity` / `eco_value` keys: v4 migration turns those into a template.

The two switches are **views** onto the water heater, which owns and restores their state from
its own `eco_use_utility` / `surplus_boost_enabled` attributes. They are not RestoreEntity: two
entities restoring one fact is the setup-order race 4.4.1 fixed for the disinfection policy.
Toggling them is a policy choice, never a manual override.

### The surplus boost only ever adds heat, and only on surplus

While surplus is active and **Boost: Use PV Excess** is on, a tank in `electric` aims at the boost
target (`_effective_target_temperature`); the published target stays the person's. Since 4.5.1 a
boost stops AT its target (no hot tolerance on top, so a 61 °C boost no longer runs to 64 °C), and
the boost target is not bounded by `max_temp`: that is the range of the everyday target dial, and
tying the two forced the dial range up just to admit a boost. Off, performance,
a shed and a disinfection cycle all exclude it, and a boost target at or below the normal target
does nothing. Felicity-style surplus detectors judge surplus from the battery, so they see the
heater's own draw: on 2026-10-18 two heaters cycled six times in an hour, 2-3 minutes each, because
their draw exceeded late-afternoon PV. Holding the element through that would drain the battery, so
there is no hold: a boost whose element was on when surplus left, within `SURPLUS_BOOST_SHORT_RUN`
(10 min) of it arriving, is locked out for `SURPLUS_BOOST_LOCKOUT` (30 min). The check runs at the
falling edge, before the new state is read, because a slow relay reports its ON after the control
pass that commanded it. Normal heating is never locked out.

### Power (mirror) is a copy and must never be counted

`ElementPowerSensor` ("Power (mirror)") copies the configured meter onto the heater's device for
the UI. Its entity id ends `_power_mirror`, never `_power`, so a template summing `..._power`
sensors cannot pick it up. It has
**no `state_class`** on purpose — the meter carries statistics and Energy-dashboard duty, and a
second statistics-bearing copy would double count. It names its source in `mirror_of`, is never
created from one of this integration's own entities, and the config flow refuses a mirror as
`power_sensor` (the selector cannot filter by platform). Consumers read the meter through the
`power_sensor` attribute, never the mirror.

### The meter is a "Linked device", declared - never written to the registry

Home Assistant 2026.9+ gives each integration its own device and lists, as **Linked devices**,
the devices that share an identifier. The heater's device carries its switch device's
identifiers, which is how the switch is listed. When the configured meter is on a different
device, `async_meter_identifiers` adds the meter device's identifiers to the heater's
**device_info**, so the meter is listed the same way. Declared, not written: 2026.9 resets a
device's identifiers to what its entities declare on every reload (measured on 2026.9.4), so a
hand-written identifier does not survive, and clearing the meter unlinks it by itself.

**Before 2026.9 an identifier cannot be on two devices**: declaring the meter's would make the
heater's entities fail to set up. `LINKED_DEVICES_SINCE` gates it (2026.6 still refuses;
2026.9.4 links; 2026.7-8 untested). The repo's test venv is 2026.10.2, so the link tests run and
the one test of the pre-2026.9 guard skips; that one needs an older core.

4.3.x used `via_device` ("Connected via") for this, which was not what was wanted. 4.4.0 clears
a link 4.3.x recorded in the `generic_water_heater.meter_links` Store, only if it is unchanged
since, and drops the record.

### A config-entry reload does not re-import module code

Only a restart loads changed Python. Stale traceback line numbers are the tell. When a change is
deployed but the old behaviour persists, that is the first thing to check, not a bug.

---

## How to work here

```bash
.venv/bin/python -m pytest -q
```

The test venv is Home Assistant 2026.10.2 on Python 3.14, the core production runs (since
2026-10-23; it was 2025.1, then 2026.9.4 for a day, before production was found on 2026.10.2).
Rebuild it with:

```bash
uv venv -p 3.14 .venv
uv pip install -p .venv/bin/python "homeassistant==2026.10.2" "pytest-homeassistant-custom-component==0.13.373"
```

**Keep it on the core this gets deployed against.** Core behaviour this suite depends on moves
between releases (listener dispatch and platform setup order both did between 2025.1 and 2026.9),
so a pass on an older core is not proof against the current one. Check the real version before
relying on any core API or internal.

### Mutation testing is the house standard for anything safety-relevant

A passing test proves nothing until you have seen it fail. Revert each guard one at a time and
confirm the matching test goes red. Two hard-won rules:

- **Commit a checkpoint first.** A mutation harness that restores with `git checkout --` after a
  failed anchor match destroys uncommitted work. Restore from a string held in memory and assert the
  file came back byte-identical.
- **Check the mutation actually ran.** Deleting a statement can leave an `if` with no body — that is
  an `IndentationError`, and a non-zero exit looks exactly like a caught mutation. Parse the mutated
  source before trusting the result.

### Record coverage honestly, including where there is none

Where a test cannot distinguish two redundant guards, or cannot reach a path at all, the docstring
says so. This is deliberate and load-bearing: a test name that implies a guarantee it does not give
is worse than a missing test, because it stops the next person looking. Existing examples:

- The three `water_heater.py` call sites that feed observed switch state to the fleet are **not
  pinned**. Removing all three leaves the suite green, because a heater that is drawing normally
  anchors itself through its own request. What they buy is the in-pass ordering race, which is
  sub-millisecond and not stageable from a test that awaits `async_block_till_done`. Three tests that
  appeared to cover them were deleted for passing with every call site removed.
- The two guards that stop a load shed reading as "target reached" are individually redundant:
  removing either alone still passes, removing both fails.

If you find one of these notes, do not "fix" it by adding a test that does not test it.

### Commit messages carry the reasoning

They are the main record of *why*, including approaches that looked right and were not. A future
reader's first question is usually "was this considered?" — answer it there. No AI attribution.

### Releasing

HACS installs **releases**, not commits on `main`. A change reaches an install only via a tag plus
`gh release create vX.Y.Z`, then a HACS update, then a Home Assistant **restart**. Bump
`manifest.json` `version` to match the tag.

### Versioning

Removing or renaming a documented option or entity attribute is breaking: bump major. Existing
config entries keep the dead keys, so add a test that an entry carrying them still loads, joins
whatever shared state it should, exposes none of the removed attributes, and can still render its
options form.

---

## Layout

| File | Holds |
| --- | --- |
| `water_heater.py` | the entity, the control loop, Smart Eco, disinfection, load-shed services |
| `fleet.py` | switch-on staggering; pure logic, imports nothing from Home Assistant |
| `config_flow.py` | sectioned config/options forms, flat storage |
| `sensor.py` | Smart Eco state, legionella risk, max-temp history, Disinfection Due In, Disinfection Hold Progress, Power (mirror) |
| `temperature_filter.py` | single-sample spike rejection; pure logic, no Home Assistant imports |
| `temperature_tracking.py` | the HA subscription wrapper every temperature consumer uses |
| `select.py` | Smart Eco Mode and Legionella Disinfection selects |
| `switch.py` | Eco: Use Utility and Boost: Use PV Excess, views onto the water heater |
| `binary_sensor.py` | Hot Water In Use draw detector |

`fleet.py` is deliberately Home-Assistant-free and driven by an injected `now`, so its decisions can
be unit tested without an event loop. Keep new decision logic there and the plumbing in
`water_heater.py`.

---

## Open items

- The surplus boost has been seen on hardware heating on surplus and stopping at its target (the
  relay opened 87 ms after the 61 C reading). Not yet seen: the short-boost lockout firing.
- **A boost does not bank disinfection holds on a sensor that reports only on change.** Idle at its
  boost target, a tank cools slowly, and a sensor that reports only on a ~0.5 C step can stay silent
  for more than `MAX_CREDITED_GAP` (30 min, `sensor.py`). `_advance_hold` credits nothing across such
  a gap, so the hold stalls while the tank sits above 60 C (seen: progress stuck at 10.8 min with no
  report for 34+ min). Performance does not hit this because the mechanical thermostat keeps the
  temperature, and so the reports, moving. A possible refinement: credit a long gap when both
  endpoints are at or above 60 C and the element was off for the whole gap (with no heat source the
  temperature can only fall, so it cannot have dipped and come back). It needs the heater's switch
  history in the risk sensor, and it loosens a deliberately conservative rule.
- `smart_eco_state` carries a relative countdown ("Resuming in 05H 59M") and is rewritten every
  minute during a timed pause (`_schedule_smart_eco_countdown_tick`), so each paused heater writes a
  recorder row a minute. `smart_eco_resume_at` already publishes the absolute deadline. Check every
  consumer of `smart_eco_state` before changing its values.
- `test_meter_link.py`'s test of the pre-2026.9 guard skips on the 2026.10.2 venv. Run it on an
  older core when touching `LINKED_DEVICES_SINCE`, or drop the guard once no supported core needs it.
- `water_heater.py` calls `device_registry.async_update_device(...,
  remove_config_entry_id=...)`; current cores warn it stops working in 2027.8.0. Move to
  `async_remove_device` / `new_config_entry_id`.
- `manifest.json` `issue_tracker` points at `ha_generic_water_heater` (underscores); the repo is
  `ha-generic-water-heater`, so the link is broken. Same for `documentation`, which points at core.
