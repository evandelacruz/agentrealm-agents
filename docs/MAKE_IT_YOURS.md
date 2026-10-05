# Make it yours

This guide is for adding your own behavior to the reference agent without reading the whole tree. You already have a character running from the [README](../README.md) quick start; here is how a **state** plugs in, how to read what the world knows, and how to test and tune what you built.

Backlog: **A51** (M13).

## How the agent thinks (one pass)

Each wall-clock window the runner makes **one HTTP call**: read self, position, terrain, entities, a zone probe, or `POST tick` with intents. What to send is chosen in two layers:

1. **`brain.choose_call`** — which kind of call fits the call budget this window ([PLAN.md](../PLAN.md) *Scheduler*).
2. **`states.dispatch`** — when the call is `POST tick`, which **state** returns the intent list.

States are checked in **priority order** (see `STATES` in `python/agentrealm_agent/states/dispatch.py`). Survival and reflexes rank above exploration. Only one state wins each decision; it returns a `StateOutcome` with zero or more intents.

## Anatomy of a state

Every state implements three methods on the `State` base class (`python/agentrealm_agent/states/base.py`):

| Method | Role |
|---|---|
| `guard(world, ctx)` | `True` when this state *may* take the round (conditions met right now). |
| `act(world, ctx)` | What to do when the state runs: return a `StateOutcome`. |
| `done(world, ctx)` | `True` when the active state should release (usually “guard no longer holds”). |

**Hysteresis:** if `memory.state` already names your state and `done` is false, your state keeps running even when `guard` would be false — until `done` becomes true or a **higher-priority** state's `guard` fires.

### `StateOutcome`

```python
StateOutcome(intents, reason, state="MyState", wait=False, reflex=False, paced=False)
```

- **`intents`** — list of intent dicts for this window's `POST tick` (see `states/intents.py`). `None` means send nothing.
- **`reason`** — short label for logs and traces.
- **`wait=True`** — intentional hold: no intent, but the round is **not** passed down (see fall-through below).
- **`reflex=True`** — this intent preempts a movement queue (same as reflex rules in PLAN.md).
- **`paced=True`** — intents are already a full paced queue (Fight); the runner sends them as-is.

### Fall-through (A44)

If your state's `guard` holds (or you are the active state) and `act` returns **no intents** and **`wait` is false**, the dispatcher tries the **next** state in priority order. Reasons from skipped states are collected in `outcome.yielded` as `"StateName: reason"`.

Use **`wait=True`** when you mean “do nothing this window on purpose” (Sync waiting for a read, Idle, Flee with nowhere to go, Recover waiting to open a chest). Do **not** use `wait` when you simply have nothing to do yet and want Explore to run — return `StateOutcome(None, "...")` without `wait` and let fall-through happen.

Tests for fall-through live in `python/tests/test_states.py` (`DispatcherFallThroughTest`).

## Where your state goes

1. Add a module under `python/agentrealm_agent/states/` with a class whose `name` is unique.
2. Import it in `states/dispatch.py` and insert an instance into **`STATES`** at the right priority — higher in the tuple means it runs earlier. Match the table in [docs/PLAYABLE_AGENT_PLAN.md](PLAYABLE_AGENT_PLAN.md) (*State machine*) unless you deliberately want to interrupt something else.
3. Export nothing special from `states/__init__.py` unless other code needs your helpers.

Keep default behavior unchanged: gate your state behind policy, directives, or a flag that is off in defaults (see the example below).

## Reading the world

**World model** (`python/agentrealm_agent/world.py`) — what this run has seen:

- `world.pos`, `world.map_id`, `world.alive`, `world.health`, `world.tick`
- `world.view.tiles` — ground and blocks in cached terrain
- `world.entities` — NPCs, characters, supplies in the last entity read
- `world.apply_events(...)` — fold tick results in tests

**PlayContext** (`states/base.py`) — per-character runtime:

- `ctx.policy` — character file goals, hostile settings, pickup, etc.
- `ctx.memory` — paths, active state name, navigation learnings, backoff timers
- `ctx.knowledge` — shared per-world JSON (`python/.state/worlds/<world>.json`): doors, terrain, investigation memory, item stats
- `ctx.directives` — hot-reloaded `characters/<name>.directives.toml`
- `ctx.plan` — strategist goal stack when present

**Knowledge base** — survives across runs for the same world. Example: `investigation.spoken_npc_ids(kb)` and `mark_npc_spoken` (called by the runner after a successful `Say`) so you do not greet the same NPC twice. Door warps and read cells work the same way (`knowledge_maps.py`, `investigation.py`).

Pathing helpers (`navigation/`, `pathing.py`, `explore.py`) build steps when you need to walk somewhere; reuse `set_position(step)` from `states/intents.py`.

## Intents and the call budget

You still get **one request per character per tick** (burst 3). Reads and tick POSTs share that budget.

- Prefer **one intent per decision window** unless you are building a paced movement queue (Explore, Travel) or Fight's attack queue.
- `Say`, `Read`, `Take`, `Use`, `SetPosition`, etc. must match the [Agent Realm API](https://agentrealm.gg/docs/api); malformed fields are rejected at ingest.
- If you need fresher entities before acting, you cannot force an entity read from inside a state — the scheduler decides. Design guards around data you already have, or accept one window of delay after `entity_refresh`.

Intent builders live in `python/agentrealm_agent/states/intents.py`.

## Testing

From the repo root:

```sh
make test
```

Patterns used everywhere:

1. Build a small grid with helpers like `tests/test_states.py` `world(...)`.
2. Wrap a `PlayContext` with `Policy`, `Memory`, and optional `Directives`.
3. Call `dispatch(world, ctx)` or your state's `act` directly.
4. Assert `outcome.intents`, `outcome.state`, `outcome.wait`, and `outcome.yielded`.

Use `unittest.mock.patch` on `dispatch.STATES` when you need an isolated priority list. Navigation fixtures under `python/tests/fixtures/` support longer walks.

Run your new test module alone:

```sh
cd python && python3 -m unittest tests.test_example_greet -v
```

## Tuning with directives

Each character can have `python/characters/<name>.directives.toml`, re-read when the file changes:

| Field | Use |
|---|---|
| `params` | Survival tuning (`retreat_hits`, `risk`, `fight_margin`, `curiosity`, …) — see PLAYABLE_AGENT_PLAN *Runtime directives*. |
| `never_attack` | Block swings at listed kinds or NPC codes. |
| `goals` | Replace the plan stack (`explore_area`, `travel:*`, `gather_gems:20`, …). |
| `instructions` | Reserved for the future strategist (M4). |
| `flags` | Boolean toggles for optional behavior (see example). |

Invalid params are ignored with a log line; a broken file keeps the last good directives.

## Worked example: greet each NPC once

The repo ships **`ExampleGreetState`** in `python/agentrealm_agent/states/example_greet.py` (~45 lines with helpers). It is wired in `dispatch.py` **above Explore** and **off by default**.

Enable it in directives:

```toml
[flags]
example_greet = true
```

Behavior:

1. `guard` — scripted, alive, on-map, flag on, and some NPC is in readable sight and not yet in `spoken_npcs` on the knowledge base.
2. `act` — `Say` hello to the nearest such NPC (`intents.say_to`).
3. After the server applies the say, the runner records the NPC in the knowledge base; the next window greets someone else or falls through to Explore.

To copy the pattern for your own state, duplicate the file, rename the class, adjust `guard`/`act`, register in `STATES`, and add a test beside `python/tests/test_example_greet.py`.

When you outgrow a flag, switch to a `goals` shorthand (like **Gather**'s `gather_gems:20`) or a planner op once you hook into the plan stack (A34).

## What to read next

- [PLAN.md](../PLAN.md) — full dispatcher order, reflexes, call budget.
- [docs/PLAYABLE_AGENT_PLAN.md](PLAYABLE_AGENT_PLAN.md) — survival params and milestone scope.
- [docs/GAME_NOTES.md](GAME_NOTES.md) — game facts with sources.
- Site [guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) — the live API your agent calls.
