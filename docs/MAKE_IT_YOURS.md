# Make it yours

This guide is for adding your own behavior to the reference agent without reading the whole tree. You already have a character running from the [README](../README.md) quick start; here is how a **state** plugs in, how to read what the world knows, and how to test and tune what you built.

Backlog: **A51** (M13).

## How the agent thinks (one pass)

Each wall-clock window the runner makes **one HTTP call**: read self, position, terrain, entities, a zone probe, or `POST tick` with intents. What to send is chosen in two layers:

1. **`brain.choose_call`** — which kind of call fits the call budget this window ([PLAN.md](../PLAN.md) *Scheduler*).
2. **`states.dispatch`** — when the call is `POST tick`, which **state** returns the intent list.

States are checked in **priority order** (see `STATES` in `python/agentrealm_agent/states/dispatch.py`): `REFLEXES`, then `EXECUTORS`, then Idle. AI plans, state machine executes ([PLAN.md](../PLAN.md) *Architecture*): a **reflex** acts on what is happening now, whatever the plan says; an **executor** runs only to carry out the plan's top op, and its guard asks `my_op(ctx, self.name)`. Explore, last, is also the safe default when there is no op. Only one state wins each decision; it returns a `StateOutcome` with zero or more intents.

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
StateOutcome(intents, reason, state="MyState", wait=False, reflex=False, paced=False, progress=True)
```

- **`intents`** — list of intent dicts for this window's `POST tick` (see `states/intents.py`). `None` means send nothing.
- **`reason`** — short label for logs and traces.
- **`wait=True`** — intentional hold: no intent, but the round is **not** passed down (see fall-through below).
- **`reflex=True`** — this intent preempts a movement queue (same as reflex rules in PLAN.md).
- **`paced=True`** — intents are already a full paced queue (Fight); the runner sends them as-is.
- **`progress`** (executors) — whether this outcome moves the plan's top op forward. Dispatch resets the op's stall clock only on progress; an op with none for 30 s is dropped (A34). Set `progress=False` on a try that has not done the job yet (a `Use` or `Compose` that may be refused). An executor that returns `wait=True` must also set `progress=False`, unless the wait itself is the job (the plan's `wait` op, Boss standing on its door): otherwise a wait that never ends pins the stack.

### Fall-through (A44)

If your state's `guard` holds (or you are the active state) and `act` returns **no intents** and **`wait` is false**, the dispatcher tries the **next** state in priority order. Reasons from skipped states are collected in `outcome.yielded` as `"StateName: reason"`.

Use **`wait=True`** only for a forced wait, “do nothing this window on purpose” (Sync waiting for a read, Idle, Flee with nowhere to go, Recover waiting to open a chest). Do **not** use `wait` when you simply have nothing to do yet and want Explore to run — return `StateOutcome(None, "...")` without `wait` and let fall-through happen.

Tests for fall-through live in `python/tests/test_states.py` (`DispatcherFallThroughTest`).

## Where your state goes

1. Add a module under `python/agentrealm_agent/states/` with a class whose `name` is unique.
2. Decide what it is. A state that reacts to what is in front of the character (and never walks off toward a goal of its own) is a reflex: insert an instance into **`REFLEXES`** in `states/dispatch.py`; higher in the tuple runs earlier. A state that does something the planner asks for is an executor: name its op in `plan.OP_STATE` (with a validator in `plan.py`), guard on `my_op(ctx, self.name)`, and insert it into **`EXECUTORS`** before Explore. Never let a state pick a movement target of its own: two states that each walk somewhere take turns moving the character.
3. Export nothing special from `states/__init__.py` unless other code needs your helpers.

That one line in `REFLEXES` or `EXECUTORS` is the switch: a state that is not in either never runs. Keep your additions in your own copy (a fork or branch) so the shipped agent stays as it is.

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

**Knowledge base** — survives across runs for the same world (`python/agentrealm_agent/knowledge_base.py`). Use `knowledge_items(kb)` for the shared `items` section (weapon stats, shop prices). Example: `investigation.spoken_npc_ids(kb)` and `mark_npc_spoken` (called by the runner after a successful `Say`) so you do not greet the same NPC twice. Door warps and read cells work the same way (`knowledge_maps.py`, `investigation.py`).

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

Invalid params are ignored with a log line; a broken file keeps the last good directives.

## Worked example: say hello to other players

The repo ships **`ExampleGreetState`** in `python/agentrealm_agent/states/example_greet.py` (about 40 lines). It says hello once to each other player in sight. No shipped state talks to players (**Greet** and Investigate say hello to **NPCs**), so it adds behavior instead of shadowing a state that already runs.

It is **not** in the shipped `STATES`, so the reference agent never runs it. It reacts to who is in sight and never moves the character, so it is a reflex. To try it in your copy, add one line to the end of `REFLEXES` in `states/dispatch.py`:

```python
from .example_greet import ExampleGreetState
...
    PickupState(),
    RecoverState(),
    ExampleGreetState(),  # mine: say hello to players in sight
)
```

Behavior:

1. `guard` — scripted, alive, and some other character is in readable sight and not greeted yet.
2. `act` — `Say` hello to the nearest one, addressed by `character_id`, and remember its id.
3. The next window greets the next player, or falls through to the plan's executor.

Things the example does on purpose, worth keeping in your own states:

- **Its own memory.** The greeted set lives on the state, keyed by your character's id because one `STATES` tuple serves every character the process runs. It needs no knowledge base, and it marks a player when the `Say` is sent, so a refused hello is not retried: at most one per player per run. (Greet and Investigate instead record NPCs in the knowledge base after an applied `Say`, which persists across runs.)
- **Pacing and budget.** It returns one `Say` per decision window. The runner sends it through the speech pacer and inside the window's `POST tick`, so it costs no extra call.
- **Priority.** Last among the reflexes, it waits for every survival state, and outranks the plan's executor for one window per player.

Its test, `python/tests/test_example_greet.py`, runs in `make test`. It checks that the shipped `STATES` leaves the example out, then patches `STATES` with the example inserted above Explore and drives the real dispatcher. Copy that pattern: duplicate the module, rename the class, change `guard` and `act`, add your line to `STATES`, and add a test beside it.

When your state needs settings, read them from the character file (`ctx.policy`) or directives `goals` (like **Gather**'s `gather_gems:20`), or hook into the plan stack (A34).

## What to read next

- [PLAN.md](../PLAN.md) — full dispatcher order, reflexes, call budget.
- [docs/PLAYABLE_AGENT_PLAN.md](PLAYABLE_AGENT_PLAN.md) — survival params and milestone scope.
- [docs/GAME_NOTES.md](GAME_NOTES.md) — game facts with sources.
- Site [guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) — the live API your agent calls.
