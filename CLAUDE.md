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

The Smart Eco Mode select follows the same rule since 4.6.1: the heater owns the mode and restores
it from its own attributes, `runtime["smart_eco_restored"]` tells the select whether to restore its
own, and `adopt_restored_smart_eco_mode` takes it without a person's side effects. Before, the
select restored unconditionally, so the platform set up last decided runtime and the select could
show one mode while the heater enforced another.

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

Leaving a parked OFF is itself a heating boundary change, and `set_operation_mode` pauses Smart Eco
for it with the timed countdown. `turn_on` pauses first, the until-satisfied way, and passes
`eco_already_paused=True` so the second pause does not replace it. Until 2026-11-16 it did, so the
until-satisfied bypass could not be reached from a parked tank, the state ON is most often pressed in.

`set_temperature` takes an `operation_mode` too (core passes it, and does not validate it for this
service). It goes through `async_set_operation_mode` only when it differs from `_current_operation`:
a client that sends back the mode it read is not a person choosing it, and through that method it
would drop a wall-switch performance run's deadline and lift a load shed. A parked tank reports OFF,
so OFF repeated changes only the target, and electric is the person's choice, pause and all.

### A load shed is not a satisfied tank

`hvac_action` reports `off` only when the *operation mode* is off. A shed deliberately leaves the
mode alone and forces only the switch, so it reads **`idle`** — identical to a tank that reached
target. Any "has it finished?" test must exclude `load_shed` explicitly.

Shedding outranks everything, including the eco policy's Always ON. It protects the supply; it is
not a user preference.

A shed is restored from the heater's own `load_shed` attribute (4.6.1), so a reload or a restart
does not end it: the balancer reads that attribute to know what it has shed, and before 4.6.1 an
options save during a shed re-energised the element while the balancer believed the supply was
relieved.

### A restart mid-dropout restores from the entity's own copy

The heater publishes `unavailable` whenever its switch is off the network, and Home Assistant saves
restore data every 15 minutes and at stop, so the last saved state can be `unavailable` with only
capability attributes. `HeaterStoredData` (`extra_restore_state_data`) is built from internal state,
which a dropout does not touch, and is used only when the published state is unusable; a usable
published state still wins. It also carries `_off_until_target_for_disinfection`, which is not
published. A new restored fact goes in the attributes (so the published path keeps working) and
reaches the stored copy for free.

### No reading, no heat — enforced in the control pass

The failsafe is `_temperature_lost`: set when the sensor *reports* unavailable/unknown or any state
that is not a finite number (`temperature_filter.finite`: `error`, an empty state, `nan`, `inf`),
cleared by the next reading, and while set `_async_control_heating` forces the switch off (past
`min_on_duration`, like a shed) before any heating branch. It lives in the control pass because
every retry and reconcile comes back through it: a one-off turn_off at the sensor event was undone
by the same pass in `performance` (which needs no temperature), and an OFF deferred by `min_on` was
retried through the missing-temperature skip and never sent. It is **not** set by "no reading yet":
setup runs a control pass before it reads the sensor, and treating that as the failsafe cycled a
heating relay off and on at every options save (`test_setup_before_the_first_reading_does_not_cycle_the_relay`).

"Not yet" is bounded, though (4.6.6). A heater that loads with no reading arms
`_first_reading_timer`; if the sensor has still said nothing after `FIRST_READING_WITHIN` (3 min),
that is a lost reading like any other. Before, the control pass skipped for as long as the sensor's
state did not change, so an element found on at a restart ran on the tank's thermostat alone, and a
sensor that is gone never changes. On a Home Assistant start the wait begins at
`async_at_started`, not at the heater's own load: the heater can load minutes before the sensor's
integration, and counting from its load would stop the element on every slow start.

What that costs and what it does not. A sensor that comes up later than 3 minutes after the start
now cycles an element that was on: off at the end of the wait, on again with the first reading.
That is the price of the bound; the routine gap is about a minute. During the wait nothing starts
heat: electric skips for want of a temperature, and `performance` skips too, since heat it started
then would have no reading behind it and the wait would only have to stop it (a reviewer's repro:
an element found off went on at load, then off when the wait ended). The lost-reading clocks start
when the wait ends, so a sensor that never speaks stops the element at about 3 minutes, logs the
warning at about 5 and raises the repair at about 13, counted from the end of the start.

Inside the wait, what the sensor says short of a number is "not yet" too (4.6.7). The owner's rule:
a restart must not operate a breaker without a good reason. `_awaiting_first_reading` is true from
just before the heater subscribes to its sensor until the first reading arrives or the wait runs
out, and while it is, `_async_sensor_changed` returns on a non-number without touching anything:
no failsafe, no control pass, and the timer neither ended nor restarted. Before, such a word was a
lost reading at once, but only if it reached the heater as a state change, which depended on whose
integration loaded first. Seen on prod: on 2026-11-18 the tank sensor's entity appeared
`unavailable` 21 s after its heater loaded and the failsafe was set; on 2026-11-16 the same sensor
was already `unknown` when the heater loaded and nothing happened. Both times its first number came
about 85 s after the heater loaded. With the element on, the first order was an off-and-on of the
relay about a minute apart (read from the code; in 7 days of history no restart found that relay
on). The flag is set before subscribing, not at the first read further down, because setup awaits
in between. It is false on a bare object: only `async_added_to_hass` opens the allowance, and the
same method registers what bounds it. On a reload that is the timer, armed at once. On a start it
is `async_at_started`, so until Home Assistant has finished starting no timer runs yet and the
bound is the start itself plus the wait: a really dead sensor and an element found on then cost
the rest of the start and 3 minutes, where 4.6.6 stopped at the first word. That is the price of
not operating the relay on a slow start, and Home Assistant bounds its own start.

Outside that wait the reading handler has no "ignore" branch. Until 2026-11-18 a value it could not
parse made it return, and `nan` became the temperature, which no comparison can act on: either way
an element that was on stayed on. Any new path that hands the heater a reading must end in a finite
number or the failsafe, never in "leave it as it was". The wait is the one exception, and it is one
only because nothing but a reading or an unload can cancel what ends it. Once a reading has
arrived, losing it is the failsafe at once, however soon after the load, and a heater that loads
with a reading never has the allowance at all (a reviewer's mutant survived until
`test_a_heater_that_loads_with_a_reading_during_a_start_waits_for_nothing`).

### A reading that does not move while the tank is heated is a lost reading (4.6.8)

"No reading, no heat" acts on a sensor that says it has no reading. A sensor that has stopped
reporting says nothing of the kind: it keeps its last number, the entity stays available, and the
failsafe has nothing to see. Seen on prod 2026-11-18: a battery Zigbee sensor behind an MQTT bridge
made its last report at 05:18 UTC, the bridge went on serving that value (and replays it after
every Home Assistant restart: unavailable, unknown, the same number), and from 10:35 the element
heated for about 90 minutes on it. Only the eco condition going false stopped it; the bound was
the tank's thermostat.

Silence cannot be the test: a sensor that reports on change is legitimately silent for 30 minutes
and more while a tank sits still (the disinfection item under Open items). The test is physical. A
tank that is being heated moves, so `reading_health.HeatingWatch` (pure, injected `now`) counts
the time the tank has been heated since its reading last moved, and at `STALE_READING_AFTER`
(30 min) the heater holds the reading as lost. The pieces, each of which a reviewer or a critic of
the first draft broke in its simpler form:

- **Heated** (`_heated_now`): there is a reading, the heater switch reads on, and either the tank
  is in the cold region (`element_health.below_any_cutout`, Element Fault's own: at or below
  min(target, 50) - 5, Celsius systems only, because the bounds are Celsius and readings are not
  converted) or the meter reads at least `DRAW_W`. The cold region needs no meter: down there no
  mechanical thermostat can be open, so a closed relay heats or is broken, and stopping is right
  for both. **The meter only adds.** The draft let a readable meter decide alone, and then a meter
  that reads nothing by mistake hid a stale sensor in a cold tank. Above the cold region only the
  meter can tell heating from a tank at its cutout with the relay closed (37 minutes of silence at
  64 C, seen), so without one a reading stuck up there is not covered.
- **Moved** is at least `STALE_READING_BAND` (0.5) from the value the count started on, either
  way. Not "differs from the reading before": two cached values served in turn differ every time
  and move nowhere, and one stray report would restart the count or release a hold. Not "rose": a
  tank drawn from harder than its element heats falls while heated.
- **The count is banked across pauses and reset only by a move.** A flapping eco condition runs
  the element three minutes at a time; a count that restarted with each run never ended. A missing
  reading is not passed to the watch at all: it neither counts nor resets, so a sensor that drops
  out and returns with the same number has not moved.
- **The hold** (`_hold_stale_reading`) is the existing failsafe, `_temperature_lost` with
  `_current_temperature` None, plus `_stale_temperature`, which is what makes it differ: the next
  number does not end it. It has its own repair (`temperature_sensor_stale`), raised at once, and
  starts neither the lost-reading warning nor the "sensor unavailable" repair.
- **It ends two ways only.** A reading that has moved from the stuck value
  (`_async_sensor_changed`), or a person asking for heat (`_retry_stale_reading`, from
  `async_set_operation_mode` with a heating mode and from `async_turn_on`, before its Always ON
  return). The second exists because the hold keeps the element off, and a tank that is not heated
  and sits at room temperature may not move half a degree for days: the owner who changed the
  battery, or reset a breaker that made a dead element look like a dead sensor, could not get heat
  back, and a restart restores the hold. It is another try, not a pardon: the number it was stuck on
  is taken at its word (not the entity's raw state, which can be a wild value the spike filter is
  still holding) and the count starts from zero. "Moved" is compared with a 1e-9 allowance in all
  three places (`reading_health.moved`): 32.3 - 31.8 is not 0.5 in floats. **Nothing restored, timed or carried
  may call it**: it switches an element on.
- **Across a restart or a reload** `stale_temperature` is restored from the attributes, but only
  when the stored copy names the same sensor (`HeaterStoredData.reading_sensor`): what was learned
  about one sensor is never held against another configured in its place, and with no stored copy
  it is dropped. The first number decides (`_take_startup_reading`, or `_async_sensor_changed` when
  the sensor is still coming up): inside the band the hold goes on, with its repair raised again;
  moved, it is released. While the sensor has not spoken the hold is pending, published, and
  commands nothing. A count that had not run out is restored the same way (`watch_value`,
  `watch_heated_s`), from the stored copy only: it moves with the clock, so it is not published.
  A restored count or hold can stop heat sooner. Neither can start any. All of this is for a
  clean stop: restore data is saved at a stop and every 15 minutes, so a hard crash forgets a hold
  that began since the last save and the element heats again for what was left of the saved count
  (a reviewer's replay; `async_dump_states` at the trip would close it and is not done). When no hold comes through
  a load (none saved, another sensor, or the reading moved) the repair is cleared there: the issue
  registry survives a reload, and after the owner configured a new sensor in place of the dead one
  the repair went on saying the old one was holding the heater off (a reviewer's repro). The
  stored count is read back as any finite number and bounded inside `HeatingWatch.restore`, which
  takes seconds for that reason: 1e300 overflowed `timedelta()` and failed the heater's setup.

This is the one place the meter decides anything (see the `power_sensor` section), and all it can
decide is that heat stops.

What it costs and what it leaves. No meter and a reading stuck above the cold region: runs to the
thermostat as before. A meter that also measures another load makes a tank at its cutout look
heated (a false hold; Element Fault reports that wiring as drawing while off). A tank that heats
slower than the band in the limit, 1 C an hour, is held wrongly. A dead element in a cold tank
with no meter is reported as a stale sensor (the heat stops either way, and the repair text says
so). On a Fahrenheit install only the meter judges. The legionella sensor reads the temperature
sensor itself, so a sensor stuck at or above 60 C can still be credited toward a hold there. A
wall-switch ON that changes the mode goes through `async_set_operation_mode` and so is asking for
heat; one pressed ON in `performance` remaps no mode and is not: the
failsafe switches it off again, as it does for a lost reading.

### `performance` never reports idle

It holds the element on unconditionally and the appliance's mechanical thermostat is what stops it,
which Home Assistant never sees. Any mechanism that waits for `hvac_action == "idle"` will wait
forever on that path and needs a time bound as well.

A wall-switch ON while `electric` would idle promotes the tank to `performance` (heat now). Until
2026-11-16 nothing ended that run: the control pass returns before reading the temperature, no eco
pause or timer touches the mode, and the promotion was written to `smart_eco_last_heating_mode`, so
the eco gate brought it back after every park. It is now bounded by
`smart_eco_manual_off_resume_hours` (`_manual_performance_until`, published as
`manual_performance_until` and restored as a deadline, never restarted), checked by a timer and at
the top of every control pass, and the promotion writes `electric` to `smart_eco_last_heating_mode`.
Two exceptions, each pinned by a test: a disinfection cycle owns its `performance` and that field, so
a flip during a cycle sets no bound and writes nothing; and any `set_operation_mode` clears the
bound, because `performance` a person chooses as the mode is deliberate and stays unbounded. A new
path into `performance` that is not a person choosing it needs the same treatment.

### Power balancing is not this integration's job (since 2.0.0)

`fleet.py` does one thing: **stagger** switch-on commands so two elements cannot step onto a shared
inverter in the same event-loop pass. Nameplate-watt admission was removed in 2.0.0 — a dedicated
load balancer owns that, and two systems balancing one supply from two sets of nameplates disagree
silently. The supported seam is the `shed` / `release` services.

**The surplus fit (4.7.0) is not an exception to this, and must not become one.** It decides one
question the balancer has no opinion on: which tank gets power that is otherwise thrown away. It
holds no budget and no sum of nameplates, never looks at the supply's capacity, and can only delay
or take back heat that exists because a surplus was announced. If a change to it would stop heat a
person, the utility, a template or the plain thermostat asked for, or would protect the inverter,
it belongs in the balancer. See "Surplus heat has to fit the surplus".

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
`unavailable`/`unknown` are never delayed — the failsafe acts on them. A state that is not a finite
number is delivered as `unknown`, so each consumer's no-reading path runs. A new consumer that
subscribes with `async_track_state_change_event` directly reintroduces the bug. The startup reads
(`hass.states.get` in each entity's `async_added_to_hass`) do not come through the helper: they
use `finite()` themselves.

### A Home Assistant start is not the eco condition going false

For `STARTUP_GRACE` (3 min since 4.6.7, the owner's choice; 2 before) after
`homeassistant_started` — not after entity load — a FALSE eco condition neither parks nor
restores; the restored mode stands. A TRUE one is trusted at once.
Switch flips in that window are not manual intent. Only a real start gets the window: a
config-entry reload has its sources loaded. Without this, every restart switched heating tanks off
because the template's source entities did not exist yet.

The boost's inputs are carried through the same window (4.6.7). The grace kept the mode, but the
boost needs `_surplus_active`, and a surplus entity that is missing, unavailable or inside its own
delay reads inactive: a tank above its normal target plus hot tolerance was "satisfied" at load, so
the setup control pass switched the element off and the boost switched it on again when the entity
read active. On prod the surplus sensor was `on` before the 2026-11-15 15:10 restart and `off` for
60 s after it. The owner's rule is that a restart must not operate a breaker without a good reason.
So a restore sets `_carried_surplus` from the published `surplus_active` and
`_carried_forecast_poor` from `forecast_preheat_active`, and inside the grace (which a reload never
has) `_surplus_for_boost()` and `_forecast_poor()` fall back on them. Each is dropped the first
time its input speaks for itself: surplus when it is read active (`_refresh_eco_entities`; from
then on an `off` is a real falling edge), the forecast when it is readable, whatever it says (at
the restore, or `_async_forecast_changed`). A readable `off` from the surplus entity does not drop
it, because that is exactly what a delayed sensor says. Only the readings are carried: the Boost
switch, a lockout, a shed and the mode are restored and decide as always.

**The carry keeps an element on; it never switches one on.** Both carried readings count only
inside the grace and while the heater's switch reads `on` (`_carry_in_force`). It took three review
rounds to get there. The first version counted the surplus whatever the relay did, on the argument
that it restored thresholds and not a relay state, and two reviewers switched an off relay on with
it: what is restored can be stale (a crash before the next 15-minute save, a power cut, hours of
downtime), and so can a person's wall-switch off under a timed pause. The second gated the surplus
and not the forecast, and a third reviewer switched an off relay on again: surplus read active,
forecast still unreadable, a carried "poor" raising the target over a tank between the two
(`test_a_carried_forecast_never_switches_an_element_on`). **Any new carried input must go through
`_carry_in_force`.** A relay that is off the network counts as not on: Home Assistant drops a
command to an unavailable entity, but an ON sent on a carried reading is not something to leave to
that.

The test is the relay as it reads now, not whether it was on when Home Assistant went down, and
nothing drops a carry when the element goes off. So an element that comes on again inside the
grace for another reason (normal heating after a draw, a person, a relay that was unavailable at
load) is kept toward the boost target until the grace ends. A latch that dropped the carry at the
first `off` was considered and rejected: relays replay states when they reconnect after a start
(2026-10-02: on then off within 250 ms with no command), and a replayed `off` would drop the carry
of an element that is running, which then cycles, the very thing this exists to prevent. The
re-adoption costs a few minutes of heat and operates nothing. There is no age check on the restored
state either: with the element-on rule a stale carry can only delay an off, and the heater's
`last_updated` could not give an age anyway (a tank at a steady temperature writes nothing for an
hour while Home Assistant runs).

`surplus_active` must stay the live reading and never the carried one: it is what the next start
restores, so a carry that published itself would be carried again by every restart inside it, and
a crash loop would boost with no surplus at all (`test_a_carry_publishes_the_surplus_it_can_see`
pins the attribute, `test_only_a_surplus_that_was_seen_is_carried` the restore). A state saved
before 4.6.7 has no such key; there `surplus_boost_active` says the same thing, since a boost was
then active only on a surplus that was read, so the restart that installs 4.6.7 is covered
(`test_the_restart_that_installs_this_version_is_covered`). The fallback applies only when the key
is absent, never when it is false. The forecast's carry can chain, because
`forecast_preheat_active` is the effective value; that only raises the target of a boost whose
surplus is real or itself carried once. `eco_gated` is false for a carried boost (below).

Costs. An element that is on may heat toward the boost target on power that is no longer surplus
from the restart until the grace ends, and the grace is counted from the end of the start, so a
slow start adds its whole length. A second restart before the surplus entity has come back carries
nothing and cycles the relay as before; one after it has been read active carries again,
correctly. That holds for clean stops. A hard crash writes no restore data (it is saved at stop and
every 15 minutes), so a loop of hard crashes restores the same stale `surplus_active` at every boot
and keeps an element that stays on until the last boot's grace ends (a reviewer's replay: five
boots 60 s apart, first off at 520 s); the boost target and the tank's thermostat bound it. A
surplus entity that reads `on` for an instant and `off` again inside the grace ends the carry. Up
to 4.6.7 it also started the short-run lockout; since 4.6.8 nothing inside the grace does (the
lockout now parks normal heating: see the surplus boost section). A relay that reads
`unknown` when the heater loads (an optimistic or MQTT switch before its first report) is not
counted as on, so it is sent the OFF and cycles as before 4.6.7; `unavailable` is sent it too and
Home Assistant drops it. Not commanding a relay that cannot be read during the grace would close
that, and is not done.

### The integration's own switch traffic is never a person

Manual-override detection in `_async_switch_changed` skips, in order: the startup window; stale
events (a later state already landed); our own commands reporting back (`_commands_in_flight`,
consumed within `OWN_ECHO_WINDOW`, 30 s — then control re-asserts intent); a switch returning from
unavailable. The echo record **must expire**: without it, a lost OFF followed by a wall OFF later
reads as our echo and the element comes back on, making OFF unreachable. Two genuine flips 50 ms
apart still count as a person — nothing distinguishes a fast replay from a hand.

A switch returning from unavailable is not a person, but it **does re-run control** (4.6.1): a
command sent while it was unreachable is dropped (Home Assistant skips unavailable entities) and a
relay can come back in either state, so before 4.6.1 a relay that reappeared ON stayed ON through a
shed or an eco park until the next temperature reading. Only a real flip (`state_changed`, not a
reappearance) stamps `_last_switch_change_time` or can be read as a manual override: an
attribute-only update (rssi, power) restarted the min on/off clock on every update, and while an OFF
was held back by `min_on` it was read as a manual ON.

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

There is deliberately no service or button that records a disinfection by hand. It would reset
the risk clock with no heat behind it, which is the false "safe" this model refuses everywhere
else. The manual reset is a real cycle: choose `Once`. (Asked for and declined 2026-11-14.)

A cycle that stands eco down (`_off_until_target_for_disinfection`) is bounded by
`DISINFECTION_BYPASS_HOURS` (8 h, 4.6.1), not `smart_eco_manual_off_resume_hours`: at 3 h a 200 L /
2 kW tank ran out of bypass short of 60 C and the cycle sat parked off, waiting for eco, until its
3-day give-up (seen 2026-11-06 to -08).

### The history sensors keep 5-minute buckets, not readings

`_Bucket` (min, max, when it peaked) per `HISTORY_BUCKET`, stored as compact lists. Raw history
cost ~600 KB of restore data per sensor and a recorder row per reading (`samples_tracked` counted
readings). The hold never reads the history: it advances from `_last_sample`, the previous live
reading, persisted separately. The 7-day sums leave out the bucket still filling and are cached
until a bucket closes; the risk signal is dispatched only when the verdict, the last disinfection or
the hold progress changes. History stored before 4.6.1 (raw samples) is read into buckets.
### `power_sensor` and `eco_gated` are a contract with another integration

`power_sensor` is the configured meter on the element's circuit (or `None`), published, and
never read live to start heat or to balance anything. (Since 4.7.0 it teaches the element's draw,
`element_draw_w`: a number remembered from past runs, which the surplus fit compares with the spare
power. The live reading still decides nothing about starting.) Two read-only diagnostics read it as evidence:
Element Fault, and since 4.6.4 the disinfection hold's element-off check. Since 4.6.8 one control
path reads it too, stop-only: `_heated_now` counts a draw as evidence that the tank is being
heated, for the stale-reading failsafe, and the only thing that can follow is the element being
held off. It can add a verdict and never remove one (the cold region does not ask it). Do not
widen that: a meter that could start heat or hold it on would make this a second power balancer. `eco_gated` is `True` only while the heat depends on the Smart Eco
condition — it would stop if the condition went false (`_eco_gated`: policy enforcing, the
condition true and the startup grace over, `electric`, no disinfection, not shed). A battery runtime estimate in another integration
leaves an `eco_gated` heater's metered draw out of what the battery must carry when the
gating supply leaves. So a false `True` hides real load: **any new way of heating regardless
of the eco condition must make `_eco_gated` false.** Boost-only heat is the one addition on the
other side: with the tank past its normal target plus hot tolerance, the boost's heat stops when
surplus leaves, which is when a battery would have to carry it, so it reads true even with the
policy off or paused. Inside the normal hysteresis band it reads false, because the element would
stay on regardless. A boost carried through a start (4.6.7) reads false too: it runs whatever the
surplus does until the surplus is read active again. Renaming or removing either attribute
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
pass that commanded it.

**Since 4.6.8 a locked-out surplus does not count for the eco gate either.** Until then "normal
heating is never locked out" stood here, on the reasoning that below its normal target a tank
heats on surplus anyway. That is exactly why the lockout did nothing on 2026-11-18: both tanks
were below their normal target, the lockout fired at every short run and withheld a boost nobody
was getting, and both relays followed 15 changes of the surplus in 100 minutes (six runs of about
3 minutes, about a minute apart). The owner's rule, no relay operation without a good reason,
decided it: a surplus that could not carry the element a minute ago cannot carry it now, whatever
the tank's temperature. So `_recompute_eco_condition` takes the surplus term as "active and not
locked", and one lockout with one deadline (`surplus_boost_locked_until`, name kept) serves both.
The utility and the template terms are untouched.

- **Trigger** (`_surplus_heating_now`): the element reads on with heat that ends when the surplus
  does. The boost, as before, or normal heating that Smart Eco allows on the surplus alone: not
  while the utility or the template also allows it (that heat goes on, so the surplus leaving
  proves nothing). Never while a lockout already runs, or an OFF held back by `min_on_duration`
  lets the flapping renew the lockout it is sitting out.
- **A run must be seen to begin to be called short.** `_handle_surplus_edge` takes `seen` (not the
  first reading after a load, not inside the startup grace) and `left` (the entity is readable and
  inactive). An unseen rise leaves the window start unknown, and `_boost_heating_since` is stamped
  only at a relay OFF to ON outside the grace, never at an attribute report, a return from a
  dropout or a state a relay replays as it reconnects. Before, a reload dated a run of hours as
  seconds old. **This is the whole of what keeps a restart from setting a lockout**, which used to
  cost a boost and would now park, when the grace ends, a tank that was heating before the
  restart: nothing in the grace is seen to begin, so nothing that ends in it or after it is short.
  The first draft also refused a lockout inside the grace and left grace heat out of the trigger;
  the mutation run showed both did nothing the two stamps did not already do, and they went. An
  entity that goes unavailable stops the heat as it always has and sets no lockout.
- When the lockout ends the condition is recomputed and control runs; a restored lockout is
  clamped to one lockout's length.
- The label `Waiting after a short surplus` shows while the gate is closed only by the lockout.

Costs, accepted. After a short run up to 30 minutes of real surplus go unused for that tank, a
cloud-sized dip included: a surplus that reads active with the element off is what both a passing
cloud and a too-small surplus look like, so there is no early release. The first short run of each
30 minutes still happens. A long run followed by a one-minute dip still costs an off and an on.
With `min_on_duration` above zero a dip shorter than it used to cost nothing and now costs an off
and, at the end of the lockout, an on. If the surplus falls a moment before the utility arrives,
the lockout is set and matters when the utility later leaves
(`test_the_utility_arriving_after_the_surplus_fell_changes_no_verdict`): each entity changes in
its own event. The restart that installs 4.6.8 inside a lockout set by 4.6.7 parks a tank that was
heating normally, once. A disinfection cycle that waits for eco is slowed by lockouts like any
other heat under the gate.

### Surplus heat has to fit the surplus (4.7.0, optional)

Decided by the owner on 2026-11-23 from three days of history: surplus heat ran 9, 0 and 21
minutes while the array was throttled. On 2026-11-23 both heaters (1850 W, 1190 W) started on a
surplus of 3.8 kW of sun less a 1.3 kW house, the pack gave 300 to 600 W, and the detector
withdrew the surplus from both after 21 minutes. On 2026-11-21 it took 5 minutes, then the
30-minute lockout sat on a full pack with the array at 0.8 kW, then 3 minutes more. The stagger
spaces starts; it never asked whether the second one fits.

Inert without `surplus_power_entity` (and the entity is ignored without a surplus entity). With
it:

- **What is sized** is `_heat_depends_on_surplus()`: heat that would stop if the surplus left.
  The gate open on the surplus alone under an enforcing policy; otherwise (no policy, or the
  utility or the template opening the gate too) only the boost's heat above what the plain
  thermostat would do (`_normal_control_heats`, which keeps the element's state inside the
  hysteresis band as the control pass does). Never during the startup grace, never while locked
  out, never while shed. `performance` under an enforcing policy is sized: the gate would stop
  it. The ON button stands the policy down, so that heat is not.
- **Admission** (`fleet.request_surplus`, asked in `_async_heater_turn_on` before the stagger and
  only for an element that is off): a 2 s window so every heater's request is in, then one
  heater per 60 s settle; **the tank closest to its goal first** (owner, 2026-11-23: one tank at
  its target is hot water, two halfway there are none; the first cut ranked the coldest first and
  was changed before it shipped). `_surplus_distance()` gives the rank: a tank below its normal
  target by its distance to that target, whatever a boost would add; a tank that has its normal
  target by what is left to its boost target and as `extra`, after every tank that has not; a
  disinfection cycle by its own goal. A rival counts only if it would itself be admitted; fits when spare >= draw x 1.1, or the reading is `curtailed`
  (a trial), or there is no reading. Refusals reuse the cooldown retry and `fleet_hold_reason`.
  **The four reason texts are constants**: they are a published attribute, and numbers in them
  wrote a row a minute for as long as a heater waited (review). The numbers go to the debug log.
- **Who is counted** (`_sync_surplus_claim`, called from the switch handler, eco input changes,
  every evaluation, the end of setup and the end of the grace): an element that is on with heat
  that depends on the surplus holds a claim, however it got there. A reload or restart forgets
  claims; they are adopted on sight, dated from the stagger's `last_admitted` where it survived.
- **Release** (`fleet.surplus_release_due`, from the power sensor's events and
  `_surplus_fit_timer`, since a stepped sensor sitting at one short value sends one event): spare
  below -150 W for 60 s (150 s when one claim is left), never inside a settle; the claim with
  the latest start goes. `_async_surplus_fit_release` sets **the existing lockout**
  (`surplus_boost_locked_until`), so the eco gate and the boost close for that heater and the
  ordinary control pass turns it off as its own echo. Term: `SURPLUS_FIT_LOCKOUT` (15 min) after
  a run of 10 minutes or more; `SURPLUS_BOOST_LOCKOUT` after a shorter one, doubled per
  consecutive failure up to `SURPLUS_FIT_LOCKOUT_MAX`, the count cleared by a lasting run or by
  the surplus leaving. A restore still clamps to one `SURPLUS_BOOST_LOCKOUT`.
- **A released heater stays counted until its relay is off** (`mark_surplus_releasing`). With a
  `min_on_duration` the OFF is held back; dropping the claim at once left a load nobody was
  counted for, the reading stayed short, and the sibling was released after it (review, twice
  reproduced). While one is releasing nobody else is picked. The exception in
  `_sync_surplus_claim` holds only while this heater still means to be off
  (`_last_commanded_switch_state`), so a person switching it back on frees the claim.
- **The label** `Waiting for more surplus` (`SURPLUS_FIT_LABEL`) covers a start waiting on the fit
  and a lockout the fit set; a restore tells that lockout from a short-run one by the stored label.

Constants and their reasons are at the top of `fleet.py` and in `water_heater_const.py`. The
release has to land before a battery-judged detector withdraws the surplus from everyone (500 W
for 3 minutes on the install): 60 s and 120 s do, and so does the single heater's 150 s.

Costs, accepted. Closest-first can leave a surplus less used than coldest-first would: on
3.8 kW of sun with a 1.3 kW house only one heater fits, and when the closer tank is the 1190 W
one the 1850 W one is tried, released, and waits. A kettle beside two running heaters parks the
last one for 15 minutes. A heater
never seen running is taken for 2000 W and may wait on a surplus it would fit until it has run
once (the utility, a person or a `curtailed` trial). A trial on a held-back array that fails
costs a minute of battery at the shortfall and a wait. With `min_on_duration` a released heater
draws until its minimum is up and `eco_gated` reads false meanwhile, as for any heat the gate
stops under that minimum. Release order after a full restart is the order the heaters loaded in.

What was mutated (2026-11-23, in a copy outside the synced folder): 70 guards across `fleet.py`,
the mixins and the config flow, all caught after tests were added for the first run's nine
survivors; then 10 more on the start order when it changed to closest-first, all caught. Reviewed adversarially by five lenses the same day; the fixes above marked "review"
came from it. Left as found: an unknown draw is assumed, not probed (a probe on any positive
spare would cycle an unmetered heater every lockout).

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

### New entities are named from the heater, never the device

`name_heater_entity` (`__init__.py`) names every helper entity and suggests its entity_id as
`<heater name> <label>`. Home Assistant otherwise builds a has_entity_name entity's id from the
DEVICE name, and devices get renamed: Disinfection Due, new in 4.6.1, came out
`sensor.<renamed device>_disinfection_due` beside siblings fixed years earlier as
`sensor.upstairs_...`. A suggestion only applies to a new registry entry, so existing ids never
move. Any new entity goes through it.

### Lasting outages and a give-up are repairs, not notifications

The temperature sensor or the heater switch unavailable for `OUTAGE_REPAIR_AFTER` (10 min) raises a
repair that clears itself on return; a disinfection give-up raises one that clears when a cycle
starts or the policy changes. Short dropouts raise nothing: a relay here drops many times a day.
The failsafe warning is logged once per outage, not per report, and only once the reading has been
missing for `FAILSAFE_WARN_AFTER` (2 min); the failsafe itself still acts at once. On a restart a
late-loading sensor goes unavailable, unknown, then reads, about a minute later (seen 2026-11-08),
which warned on every restart before. `async_remove_entry` deletes this
heater's repairs. The "Off until target" time bound still notifies: it is an event, not a
condition to fix.

### Publish deadlines, never time remaining

A timed Smart Eco pause keeps one `smart_eco_state` label for its whole length (`Paused by manual
control`, or `Always ON override (Paused by manual control)`); when it ends is
`smart_eco_resume_at`, an absolute time a frontend counts down to by itself. Until 4.6.0 the label
was a countdown refreshed every minute, which rewrote the heater's state and the Smart Eco State
sensor's: two recorder rows a minute per paused heater, for up to 48 hours. Anything that changes
only because the clock moved does not belong in a state or an attribute. An integration cannot keep
its own entity's state out of the recorder (`_unrecorded_attributes` drops attributes, not the
row), so "exclude it from the recorder" is not a fix. `test_smart_eco_pause_writes.py` counts the
writes through a pause, and through a reload mid-pause: the restore branch armed the old tick
separately.

### A config-entry reload does not re-import module code

Only a restart loads changed Python. Stale traceback line numbers are the tell. When a change is
deployed but the old behaviour persists, that is the first thing to check, not a bug.

---

## How to work here

```bash
.venv/bin/python -m pytest -q
```

The test venv is Home Assistant 2026.10.2 on Python 3.14. Production has run 2026.11.0 since
2026-11-05, but on 2026-11-08 no `pytest-homeassistant-custom-component` release supported it
(the newest, 0.13.375, pins 2026.10.4, and forcing 2026.11.0 under it fails to import: 2026.11
aliases `httpx` to `httpx2`). 4.6.1 was run on 2026.10.2 and 2026.10.4. Move the venv to 2026.11
as soon as a release for it exists. Rebuild it with:

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
- **Run the harness on a copy outside any synced folder** (a `git worktree` in a temp directory,
  with the checkout's venv). In a synced folder, files rewritten in quick succession came back with
  an earlier write's content: on 2026-11-18 two mutants were found still in place after a run whose
  every restore had read back byte-identical, and later mutants were "caught" by the leftovers.
  Afterwards, compare the real checkout against a saved `git diff HEAD`.
- **Never `git stash` while worktrees are in use.** The stash belongs to the repository, not the
  worktree: a pop takes whatever is on top. Agents told to stash in parallel worktrees swapped each
  other's changes, and one landed in the main checkout. Take a change out with a patch file instead
  (`git diff > x.patch`, `git checkout -- <paths>`, `git apply x.patch`).

### Record coverage honestly, including where there is none

Where a test cannot distinguish two redundant guards, or cannot reach a path at all, the docstring
says so. This is deliberate and load-bearing: a test name that implies a guarantee it does not give
is worse than a missing test, because it stops the next person looking. Existing examples:

- The three call sites (now in `water_heater_switch.py` and `water_heater.py`) that feed observed switch state to the fleet are **not
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
| `water_heater.py` | setup, the entity class (restore, properties, service entry points); its behaviour lives in the mixins below |
| `water_heater_switch.py` | the control loop, switch commands, cooldown, fleet admission, failsafe and outage timers, load shed |
| `water_heater_eco.py` | Smart Eco: eco condition, pause/resume, off-until-target |
| `water_heater_disinfection.py` | disinfection cycles: start, end, give-up |
| `water_heater_surplus.py` | surplus boost, forecast pre-heat, the heating goal, the surplus fit's heater side |
| `water_heater_const.py` | timings and constants the mixins share (re-exported from `water_heater.py`) |
| `fleet.py` | switch-on staggering and the surplus fit's decisions; pure logic, imports nothing from Home Assistant |
| `config_flow.py` | sectioned config/options forms, flat storage |
| `sensor.py` | Smart Eco state, legionella risk, max-temp history, Disinfection Due In, Disinfection Hold Progress, Power (mirror) |
| `temperature_filter.py` | single-sample spike rejection; pure logic, no Home Assistant imports |
| `temperature_tracking.py` | the HA subscription wrapper every temperature consumer uses |
| `select.py` | Smart Eco Mode and Legionella Disinfection selects |
| `switch.py` | Eco: Use Utility and Boost: Use PV Excess, views onto the water heater |
| `binary_sensor.py` | Hot Water In Use draw detector; Element Fault |
| `element_health.py` | Element Fault's decision (meter vs relay); pure logic, no Home Assistant imports |
| `reading_health.py` | the stale-reading count (heated time since the reading last moved); pure logic |
| `heat_rate.py` | heat-up rate learning and the Ready At estimate; pure logic |
| `energy_split.py` | today's element energy per source; pure logic |
| `insights.py` | the Heat-up Rate, Ready At and Energy Today entities (plumbing for the two above) |
| `diagnostics.py` | config-entry diagnostics: options, fleet member, heater internals, timers |

`fleet.py` is deliberately Home-Assistant-free and driven by an injected `now`, so its decisions can
be unit tested without an event loop. Keep new decision logic there and the plumbing in
the `water_heater*.py` modules. `heat_rate.py`, `energy_split.py`, `element_health.py` and
`reading_health.py` follow the same pattern.

---

## Open items

- **Surplus fit (4.7.0, written 2026-11-23): released, installed, not yet loaded.** Released as
  v4.7.0 on 2026-11-23 and installed on the owner's install the same day (every file checked
  against the release); it loads at the next Home Assistant restart, which is the owner's to do.
  841 tests pass. A read of the release notes against the code before publishing corrected one
  README sentence (what happens while the sensor has no reading). It does nothing until
  `surplus_power_entity` is set on each heater, which on the owner's install waits for the
  inverter integration's `PV Surplus Power` sensor (its 0.3.33, installed there the same day
  and waiting for the same restart). To see on hardware once both are live
  and configured: on a surplus one heater starts, the second a minute or more later and only if
  the reading allows; `fleet_hold_reason` and `Waiting for more surplus` on the one waiting;
  `element_draw_w` near 1850 and 1200 after each has run once; on a shortfall one relay
  operation, not two, and the other heater's `smart_eco_state` staying `Heating in eco`. Not
  decided: whether the unknown-draw assumption needs a configured fallback for an unmetered
  heater (the owner's are both metered).

- `water_heater.py` serves temperatures under both the `native_*` names (read by 2026.11, where the
  plain ones are deprecated and stop working in 2027.11) and the plain names (all 2026.10 has).
  Drop the plain three once `hacs.json`'s minimum core is 2026.11 or later.
- The surplus boost has been seen on hardware heating on surplus, stopping at its target (the relay
  opened 87 ms after the 61 C reading) and locking out twice in one afternoon, the element starting
  again at the exact moment a lockout ended. The weak spot seen that afternoon (the short window
  measured only from surplus arriving, so the first short boost after a long sunny stretch was not
  locked out) is closed in 4.6.2: the window starts at the later of surplus arriving and the boost's
  element coming on (`_boost_heating_since`). Not yet seen on hardware.
- Element Fault (4.6.2) is new and unproven on hardware. Its thresholds (100 W drawing, 20 W idle,
  3 / 10 min holds, no-draw only below min(target, 50 C) - 5 C) are guesses sized to keep a
  mechanical thermostat's cutout from reading as a fault.
- **Since 4.6.0 a long silent gap can count toward a disinfection hold** (`MAX_ELEMENT_OFF_GAP`,
  `sensor.py`): up to 2 h, when the heater switch stayed off for the whole gap (`_element_off_since`:
  off now and `last_changed` no later than the gap's start) and both readings are at or above 60 C.
  Before it, a sensor reporting only on ~0.5 C steps went silent past `MAX_CREDITED_GAP` (30 min)
  while a tank sat idle at its boost target, and the hold stalled (seen 2026-10-23: 10.8 min, then
  34+ min of silence). The window, growth and kill-credit sums in `_recalculate` still skip gaps
  over 30 min: only the hold has the exception.
  Since 4.6.4 the configured meter is evidence too: below `NO_DRAW_W` now and `last_changed` no
  later than the gap's start. The switch alone cannot see the commonest case: PERFORMANCE and a
  disinfection cycle run to the tank's mechanical thermostat, which opens the circuit with the
  relay still closed (seen 2026-11-14: 54.2 min banked, cutout at 64.7 C with the relay on, 37 min
  of silence at 64+ C, hold stalled). The meter is credited only for the hold, never for control.
  A tank with no meter still stalls this way. 4.6.4 has been live on prod since the 2026-11-14
  restart; neither path yet seen completing a hold on hardware.
- 4.6.7 has been live on prod since the restart of 2026-11-18 14:12 UTC, on Home Assistant
  2026.11.2 (the tests still run on 2026.10.2). Diagnostics carry `awaiting_first_reading` and
  `boost_surplus_carried`, and the log has nothing from the integration. Neither relay moved,
  across the restart or when the 3 minutes ended. What that restart did and did not exercise:
  - One tank was mid disinfection cycle with its element on. It came back in the cycle with
    `disinfection_started_at` kept, and the relay stayed closed throughout. Its sensor already had
    a number when the heater loaded, so there was no wait.
  - The other tank was parked by Smart Eco, element off. Its sensor said `unavailable` 0.3 s after
    the heater loaded, `unknown` 12 s later, and gave a number 69 s after the load. The heater
    wrote no state for either word and has no failsafe set. That is the "not yet" path, but
    inferred: `temperature_lost` is recorded nowhere, and with the element off no relay would have
    moved in 4.6.6 either. The case the change was written for, those words with the element on,
    has still not been seen.
  - The boost carry was not exercised: there was no surplus before or after the restart.
  - That "first number" was not a reading. The sensor is a battery Zigbee device behind an MQTT
    bridge, and the bridge replays the device's last cached value when Home Assistant comes back.
    The device itself had last reported nine hours earlier (next item). So the first-reading wait
    can be ended by an old value, and so could the 09:40 one below.
- **4.6.8 has been live on prod since the restart of 2026-11-18 21:19 UTC**, on Home Assistant
  2026.11.2 (the tests still run on 2026.10.2). Both heaters came back parked by Smart Eco with
  their state, no `stale_temperature` and no lockout; neither relay was commanded (one switch
  entity was `unavailable` for 5 s and came back `off`, as it was), and the log has nothing from
  the integration. Neither change has been seen doing its job on hardware yet: there was no
  heating and no surplus at that hour. Both came out of what prod showed on 2026-11-18:
  - The stale-reading failsafe ("A reading that does not move while the tank is heated is a lost
    reading"). The incident: one tank's sensor made its last report at 05:18 UTC and its bridge
    went on serving 28.9 C through two restarts; from 11:00 UTC the element drew about 1.19 kW
    for 65 minutes in all (metered; the relay was already closed at 11:00) with the reading
    unchanged, on a sensor that climbs 5-6 C an hour under that element. What to look for: with a
    sensor that has stopped reporting and the element on, the element stops after 30 minutes of
    heating, the repair appears, `stale_temperature` holds the number, and a restart keeps it so.
    Its limits (30 min, 0.5, the cold region's bounds borrowed from Element Fault) are sized to
    the two tanks of the real install and are guesses for any other.
  - The lockout for normal heating ("The surplus boost only ever adds heat"). The incident:
    between 11:15 and 12:56 UTC the surplus sensor changed 15 times and each of the two relays
    followed every change, 15 operations each in 100 minutes; six of the runs lasted about 3
    minutes with about 66 s between them. Replayed against 4.6.8 that trace costs each relay 7
    operations. What to look for on a marginal afternoon: one short run, then
    `Waiting after a short surplus` with `surplus_boost_locked_until` set, and no relay movement
    until it passes.
  - Not built, known: a sensor stuck at or above 60 C can still be credited toward a disinfection
    hold, because the legionella sensor reads the temperature sensor itself and knows nothing of
    the heater's hold. A restart makes it worse (the bridge replays the number as a fresh
    reading, and the element-off gap rule can then credit up to 2 h).
- 4.6.6 was live on prod from the restart of 2026-11-18 09:40 UTC to the one at 14:12 UTC, on Home
  Assistant 2026.11.2 (the tests still run on 2026.10.2). Both heaters came back as they were,
  parked by Smart Eco, with their state, and the log has nothing from the integration. None of its
  fixes has been seen on hardware yet. What to look for: a surplus boost on a tank with cold
  tolerance 0 stops at exactly the boost target and stays off; Ready At moves at once when the
  target changes mid-run.
  That restart never armed the first-reading wait. One tank's sensor entity did not exist when its
  heater loaded, appeared `unavailable` 21 s later (a few seconds before Home Assistant finished
  starting, so before the wait would have been armed), turned `unknown` 45 s after that and gave
  its first number 20 s later, about a minute after the start finished. In 4.6.6 `unavailable`
  arriving as a state change was a lost reading, so the failsafe was set at once and the first
  number cleared it; the element was off, so no relay moved. 4.6.7 makes those words part of the
  wait (see "No reading, no heat").
- 4.6.5 has been live on prod since the restart of 2026-11-16 22:04 UTC. Both heaters came back
  with their state (one mid timed pause, its deadline kept) and publish `manual_performance_until`.
  Neither fix has been seen on hardware yet. What to look for: a wall-switch ON on a tank above
  target minus the cold tolerance moves it to `performance` with `manual_performance_until` set
  `smart_eco_manual_off_resume_hours` ahead, and it returns to `electric` at that time; ON pressed
  on a tank the eco gate has parked reads `Paused until the tank is satisfied`.
- 4.6.3's insights and forecast pre-heat have been live on prod since the 2026-11-09 restart, but
  not yet seen doing their job on hardware (no heating run had happened yet):
  - Heat-up Rate learns only from runs of 10+ minutes that rise 1+ C, with a 0.3 moving average.
    "Drawing" is the meter at 100 W or more, or the switch on when there is no meter. A shower
    mid-run reads slow; the average absorbs it rather than rejecting the run.
  - Energy Today credits each interval at the power and source at its start, flushes a steady
    draw every minute, and credits nothing across a gap over 10 min (a restart).
  - Forecast pre-heat raises the boost target, so everything the boost obeys (surplus only, the
    Boost switch, the short-run lockout, electric only) bounds it too.
  - Switching an insight off deletes its registry entries (`remove_switched_off_entities`);
    switching it back on brings the same entity ids back, without any customisation.
- The heater shows unavailable only after its switch has been away for
  `SWITCH_UNAVAILABLE_AFTER` (2 min). Inside that window it keeps controlling: commands to an
  unavailable switch fail and the next pass retries. The outage repair is still at 10 min.
  Seen on hardware once: a 1 s relay dropout at startup left the heater available.
- AA's own dashboard has a `custom:button-card` template (`water_heater_tank`) that builds sensor
  ids from the heater's object id plus fixed suffixes (`_ready_at`, `_smart_eco_state`,
  `_energy_today_*`, `_element_fault`, `_hot_water_in_use`, `_days_until_disinfection`,
  `_legionella_risk`) and reads the `heading_for` attribute and the heater's `disinfection_active`
  and `surplus_boost_active` attributes. Renaming any of them breaks that card silently.
- `test_meter_link.py`'s test of the pre-2026.9 guard skips on the 2026.10.2 venv. Run it on an
  older core when touching `LINKED_DEVICES_SINCE`, or drop the guard once no supported core needs it.
- An external review (2026-11-16) was checked finding by finding: a code trace, a repro test, and a
  second reviewer trying to overturn each verdict. 4.6.5 fixed its two real behaviour bugs. 4.6.6
  fixed the eight low ones (none seen on hardware yet), each with a test that fails without it:
  - A surplus boost stops at exactly its target with cold tolerance 0 (the stop is tested first
    while boosting; outside a boost the order is unchanged).
  - Ready At follows a changed goal at once. The heater overrides `_async_write_ha_state` (the
    method core names for it; `async_write_ha_state` is final) and sends `heat_rate_signal` when
    `heating_goal()` differs from the last goal it announced. Every input of the goal is published,
    so a change always comes with a write.
  - Heat-up Rate's unit follows the unit system (`°F/h` on a Fahrenheit install).
  - `set_temperature` honours `operation_mode` (see the Smart Eco section above).
  - A tank state that is not a finite number is a missing reading everywhere (see "No reading, no
    heat").
  - `to_watts` reads every Home Assistant power unit at its own scale, plus `w`, `kw`, `KW`, `Kw`,
    ignoring surrounding whitespace. Still no casefold: `mw` and `Mw` are not guessed, and a unit
    nobody can interpret is still taken as watts.
  - The fleet object is kept while it holds unexpired departed anchors (`HeaterFleet.holds_state`),
    so a reload of every entry at once keeps the stagger.
  - Diagnostics list every `*_timer` the heater holds, and a test compares the list with the
    entity so it cannot fall behind again.

  Found while fixing them, all older than this change, and open:
  - **The integration is Celsius-only.** Readings arrive in the unit system's unit and are compared
    with constants that are in Celsius, unconverted: the disinfection thresholds in `sensor.py`,
    `DISINFECTION_GOAL_C`, the spike filter's 10, Element Fault's 50 and 5, Hot Water In Use's
    rates, the heat-rate bounds, and the config flow's default range (15 to 80). Reproduced on a
    US-customary system before 4.6.6: an hour at 61 F set `last_disinfection_at` and Disinfection
    Due In read 10 days, a false "disinfected". Since 4.6.6 the README says so, and where
    `celsius_system(hass)` is false the Legionella Risk sensor is inert (Unknown, nothing restored,
    no samples) and the heater refuses every policy but Off, drops a restored one and ends a
    restored cycle: with the sensor inert nothing could complete a cycle, and a one-shot would hold
    performance until the give-up bound. The unit is read when the entities load, so the entry
    reloads when the unit system's temperature unit changes (`async_setup_entry`). The heater and
    the policy select each restore the policy and hand it to the other, in either platform order:
    every guard on that path is tested in both. The other constants are still unconverted there.
    Converting readings to Celsius where they meet those constants (the tracking helper delivers
    each reading with its unit) is the real fix: roughly 100 to 150 lines, plus a decision on the
    history a Fahrenheit install has already stored.
  - Fixed in 4.6.6 too: a restart with the element on and no reading now enters the failsafe after
    `FIRST_READING_WITHIN` (see "No reading, no heat"). Not seen on hardware. 4.6.7 widened the
    wait to a sensor that says unavailable or unknown before its first reading.
  - Fixed in 4.6.7: a restart during a surplus boost or a forecast pre-heat cycled the relay (found
    by the review of 4.6.7's first change; see "A Home Assistant start is not the eco condition
    going false"). Not seen on hardware.
  - A source slower than `STARTUP_GRACE` after a start still parks an element that was on and
    restores it when the source arrives, and a boost carried through the grace ends with it the
    same way. `test_the_grace_is_bounded_and_eco_then_parks_the_tank` pins the park, so this is
    the bound working as designed; on prod the sources are back about a minute into the 3.
  - If `async_added_to_hass` raises after the first-reading timer is armed, the abort does not
    cancel it (`async_will_remove_from_hass` is not called on an abort): it fires when the wait
    ends and raises on `hass` being None. No relay is commanded. A reviewer's forced repro; whether the
    other timers armed during setup share this was not checked.
  - Switching the legionella sensor off in the options while a cycle runs looks unhandled (read,
    not tested): the select and the sensor go, the heater restores the policy and the cycle, and
    nothing can complete it before the give-up bound.
  - `believed_on` carried across a reload is not corrected when the switch is found off on the way
    back, whatever `DepartedAnchor`'s docstring says: that heater's next switch-on is admitted
    without a new anchor, so a sibling can follow it sooner than the stagger by the length of the
    reload. True of a single entry's reload too.
  - The Power (mirror) sensor raises `ValueError` in its state write when the meter's state is
    `nan` (a `power` sensor may not hold a non-finite value). It keeps its last value; the error
    is logged.
  - A Heat-up Rate restored after the unit system was changed keeps its old number under the new
    label until a few runs relearn it.

  Checked and wrong, so not to be re-derived: the short-run boost lockout being skipped after a
  restart (`_surplus_window_start` is always set before `_boost_heating_since` can be), and the
  meter's `last_changed` check defeating the 4.6.4 hold credit. The second is true only of a meter
  that jitters while idle, and it stalls the hold, the safe direction; judging by the current
  reading alone would credit a gap the element drew in.

## Cloud and web sessions

A session that runs in a cloud sandbox (Claude Code on the web, a scheduled routine, an `ultra`
review) does not load the maintainer's global instructions and cannot reach the lab: no LAN or
tailnet route, no memory notes, no vault. These rules stand in for the global ones there. All
but the branch rule bind local sessions too.

- **No AI attribution** in commit messages, PR bodies or comments: no `Co-Authored-By: Claude`
  trailer, no "Generated with Claude Code" footer. This overrides the harness's own attribution
  reminder.
- **In the cloud, work on a branch and open a draft PR. Never push to `main`, never merge.** The
  maintainer validates on real hardware before anything lands.
- **Sanitize before the first push.** Grep the diff, any new fixtures and the commit messages for
  private and tailnet addresses (`192.168.`, `10.x.x.x`, `172.16-31.`, `100.64-127.`), `.home`,
  `.lan`, `ts.net`, MAC addresses, and anything that looks like a real hostname, domain, SSID or
  person's name. Use `192.0.2.x` / `198.51.100.x`, `aa:bb:cc:dd:ee:NN` and generic names. A
  force-push does not take a leak back: GitHub keeps the old commit reachable.
- **PR bodies and comments: one paragraph per line.** GitHub renders a single newline as a line
  break, so hard-wrapped prose arrives broken mid-sentence.
- **Nothing can be deployed or tested live from the cloud.** Skip deploy steps, and say in the PR
  what still needs checking on a real install.
