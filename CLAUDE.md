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

### A config-entry reload does not re-import module code

Only a restart loads changed Python. Stale traceback line numbers are the tell. When a change is
deployed but the old behaviour persists, that is the first thing to check, not a bug.

---

## How to work here

```bash
.venv/bin/python -m pytest -q
```

**The test venv's `homeassistant` is several major versions behind what this gets deployed against.**
The suite passing is not proof against a current core. Check the real version before relying on any
core API or internal.

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
| `sensor.py` | Smart Eco state, legionella risk, max-temp history |
| `select.py` | Smart Eco Mode and Legionella Disinfection selects |
| `binary_sensor.py` | Hot Water In Use draw detector |

`fleet.py` is deliberately Home-Assistant-free and driven by an injected `now`, so its decisions can
be unit tested without an event loop. Keep new decision logic there and the plumbing in
`water_heater.py`.
