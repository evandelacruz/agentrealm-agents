# Reference agent: plan

A test agent for creating characters and running them against an Agent Realm world. It is also the seed of the public [reference agent](https://agentrealm.gg/) the site quick start calls for: Python first, an example rather than an SDK.

It is outside the formal backlog. It is built interactively and changes as the API does.

## Boundaries

- **An ordinary API client.** HTTP only. It imports nothing from the Go code and never touches Postgres, Redis, or NATS. If it needs something the API does not give, that is a server gap, written down below, not a side door.
- **Python 3.11+, standard library only.** No install step beyond Python. The LLM planner (M4) is the one place a dependency may enter, and it stays optional.
- **Obeys the invariants a client can see.** At most one intent per tick, no standing orders, and when there is no decision it sends nothing.

## What the API gives today

| Call | Use |
|---|---|
| `POST /worlds/{code}/characters` `{name, avatar, model_agent}` | Create. 201 returns self. |
| `GET /characters/{id}/self` | Lives, health, perception range, movement speed, alive, placed. |
| `GET /characters/{id}/position` | `{map_id, x, y}`. 409 `not_on_map` until placed. |
| `GET /characters/{id}/world` | Tick rate, sandbox flag, status. |
| `GET /characters/{id}/terrain-tiles?map_id&x0&y0&width&height` | Block types inside perception, plus revealed ground, as a grid: `rows` of `legend` symbols, `?` for clouds. |
| `GET /characters/{id}/entity-tiles?…` | Characters, NPCs, supplies inside perception. |
| `POST /characters/{id}/tick` `{"intents": [{...}]}` | Replaces the character's queue with a one-intent list. Returns `queue_id`, `intent_results` since the last call, events by tick, dropped count, the observation, and the clock. |

Auth is `Authorization: Bearer <key>`.

**One request per character per tick, with a burst of 3, on every character route.** Reads count. The limiter is a token bucket refilled on the front's tick interval ([Manual §7.4](https://agentrealm.gg/docs/manual#74-rate-limits)). So the agent runs a call budget: each tick it spends its call on the read it most needs or on the tick submit. The round trip carries the observation (B15), but this agent reads only ground chest contents from it, so its terrain and entity reads compete with intents for the same budget.

The runner paces one call per wall-clock window (`epoch / tick interval`). That is stricter than the bucket requires: safe, but slower than it could be.

## Real time

Worlds run at 10 ticks per second by default. The agent's shape already fits: the planner is the slow loop and writes plans off the tick, and the reflexes are the fast executor. The intent queue (B98) lets one request carry up to four seconds of intents, run one per tick; the agent sends one-intent queues. With longer ones, the executor would keep a few seconds queued and send a new queue when a result or delta makes the old one wrong. `run` does not send `Sleep` (B45) when it stops, so a stopped character stays standing until auto-sleep takes it off the map. See [Real-time play](https://agentrealm.gg/docs/guides/create-a-character-agent#real-time-play).

## Architecture

```
            ┌──────────── one call per window ────────────┐
 scheduler ─┤ read position | read terrain | read entities | POST tick(intent) │
            └──────────────────────┬──────────────────────┘
                                   ▼
                              world model
                   (per map: tiles, occupants, age of each read;
                    self; recent events; last intent + result)
                                   ▼
   planner  ──sets──►  goal + settings  ──►  plan (path)  ──►  reflexes  ──►  intent or none
 (scripted / llm,                              A* over known                (every window,
  slow, off-tick)                              walkable tiles                no model)
```

### Scheduler: which call this window

Checked top to bottom:

1. Position unknown, or a door or death may have moved us → read position.
2. Terrain around us is stale (map changed, or we moved more than half the perception range since the last terrain read) → read terrain.
3. Entities are older than the character's `entity_refresh` ticks, or a `Damaged`/`Attacked` event just arrived → read entities.
4. Otherwise → `POST tick` with the chosen intent, or with none.

Self is re-read after `Died`, and every 60 windows otherwise.

Position is tracked locally: a submitted `SetPosition` moves us to the target at once, because its result only arrives with the next submit. A rejection puts us back where we stood before it, and the intent submitted in that same round trip was planned from the refused step, so it is not assumed. A rejection, a door, or a death sends us back to step 1.

`Attacked`, `Damaged`, and `Died` on a queue are always ours: they carry no `subject_id` there.

### Reflexes: every window, no model

The first rule that matches picks the intent:

1. Previous intent rejected → clear the path. The next decision keeps off that block, in the replan too.
2. Standing on a block in `avoid_blocks` → step to the nearest safe neighbour.
3. Hostile in range: `on_hostile = "flee"` → step to the neighbour farthest from it. `"fight"` → `Use` on it.
4. Supply underfoot or adjacent and `pickup = true` → `Take`.
4b. Our last death dropped a chest on this map and `pickup = true` → walk to it; on or next to it, `WithdrawFromChest` with only its `chest_id`, which takes everything that fits, until the snapshot shows it empty or gone. `Died` names the chest and where it landed; this agent sends no `snapshot_version`, so every round trip carries a complete snapshot with the chest's `contents`.
5. Plan has a next step → `SetPosition` there.
6. Otherwise → nothing.

"Hostile" and "in range" read from settings: the API serves no hostile's reach and no other character's health.

### Plan: goal and path

A goal plus an A* path over tiles this character has seen and knows are walkable. Unknown tiles are never pathed through. Occupied tiles and `avoid_blocks` are avoided. Standing on an `avoid_blocks` tile with no safe step off, the plan may cross them, as few as it can. Movement is Chebyshev: diagonals cost the same as straight steps.

| Goal | Target |
|---|---|
| `explore` | Nearest frontier: a known walkable tile next to an unknown one. |
| `doors` | Nearest known door (`framed_door`, `rock_entry`). Stepping onto one warps. |
| `goto` | A fixed `(x, y)`. |
| `hold` | Stay. |
| `wander` | A random walkable neighbour. |

Goals are tried in order; the first with a reachable target wins. Re-plan when a step is rejected, when the map changes, or when a terrain read disagrees with the path.

### Planner: sets goals and settings

- **scripted**: goals and settings come straight from the character file.
- **llm** (M4, superseded by the strategist in [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md)): every N ticks, or when something new happens (new character seen, door found, goal exhausted, damage), it sends the model a summary of the world model and gets back goals, settings, and an optional `Say`/`Broadcast`. It runs off the tick loop. The loop keeps the old answer until a new one lands, so a slow model never costs a tick.

## Character file

One TOML file per character. `tomllib` is in the standard library.

```toml
name = "Wren"
avatar = "default"                  # outfit code
model_agent = "agentrealm-reference/scripted"
world = "sandbox"

[policy]
kind = "scripted"                   # idle | wander | scripted (llm is M4, not built)
goals = ["explore", "doors"]
on_hostile = "flee"                 # flee | fight | ignore
hostile = ["npc"]                   # npc, character
hostile_range = 2
pickup = true
avoid_blocks = ["fire", "lava"]
entity_refresh = 5                  # ticks between entity reads when calm
```

Created character IDs are saved in `python/.state/<name>.json` (gitignored), so `run` finds the character again.

## CLI

```
python -m agentrealm_agent create characters/wren.toml
python -m agentrealm_agent run characters/wren.toml [characters/kit.toml ...]
python -m agentrealm_agent status characters/wren.toml
```

Environment: `AGENTREALM_BASE_URL` (default `http://localhost:8080`, a local stack; the public API is `https://api.agentrealm.gg`, where lives are permanent), `AGENTREALM_API_KEY`. After `make up` in the game repo, run [`scripts/seed_local_stack.py`](scripts/seed_local_stack.py) with `AGENTREALM_STACK_DIR` pointing at that compose project so the front tier logs yield a verification token; it mints the first key and waits until sandbox `create` succeeds (Manual §13).

`run` drives every listed character, one thread each. Each character logs one line per window to stdout (tick, position, call made, intent, the result of the last one, events) and a JSONL trace to `.state/<name>.trace.jsonl`, so a death can be read back as a decision.

## Server gaps that limit the agent

| Gap | Effect on the agent | Where it lands |
|---|---|---|
| Sandbox content loads on the sim's first start; the `default` outfit is seeded | Create works with avatar `default` once the sim has started; before that it answers `world_not_ready`. | B13, B39 |
| No hostile's reach is served | `hostile_range` is a guess in the character file. | None |
| No NPC's health or damage is served, except a boss's health | Hostile health and damage per type are learned from `NPCDamaged`, `NPCDied` and `Damaged`, with conservative defaults until measured ([`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) Combat). | None |
| Our own strength is served only on the owner watch sheet, not on a character route | The agent brackets its strength from `over_strength_ceiling` rejections and does not read the watch sheet, which sits outside the character call budget. | None |

## Milestones

These are the backlog. Each has a stable ID; cite it in commits and PR bodies. Per-ID state lives in [`status.json`](status.json), which is not a source of truth: where it disagrees with this file, status is wrong.

| ID | Milestone | Depends on |
|---|---|---|
| M0 | **Discovery.** Docs read, hand play through MCP, [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md) written. | |
| M1 | **Client and loop.** HTTP client, call scheduler, wall-clock pacing, 429/503 handling, `create`/`run`/`status`, `idle` and `wander`. | |
| M2 | **World model and pathing.** Tile and entity cache per map, local position tracking, A*, `explore`, `doors`, `goto`. | M1 |
| M3 | **Reflexes and scripted characters.** The reflex list, the full character file, the trace. | M2 |
| M4 | **Strategist and directives.** The LLM planner: optional dependency, off-tick, emits typed plan operations, never intents. Plus the runtime directives file. Specified in the playable plan. | M9 |
| M5 | **Local seed.** A script that gives the local stack an account, a key, and a playable sandbox map, so `create` works end to end. The `default` outfit is already seeded by migration 00023. The agent's default base URL is the local stack. | M1 |
| M6 | **Executor.** `Step`/`Wait` paced multi-intent queues, two poll cadences, snapshot deltas, health tracking. | M3 |
| M7 | **State machine and survival.** Prioritised states replace `brain.decide`; cost-grid navigation, stuck detection. | M6 |
| M8 | **Gear, economy and combat.** `Gather`, `Shop`, `Loot`, `Equip`, `Fight`; learned item table. | M7 |
| M9 | **Navigation and knowledge.** Per-world knowledge base, `Travel`, door graph, `Break` and break memory. | M8 |
| M10 | **Curiosity and clues.** Interest list, odd-block detector, `Investigate`, clue capture. | M9 |
| M11 | **Levels.** `Level`, `Boss`, `Solve`. | M4, M10 |
| M12 | **Evaluation.** Metrics per run, compared across commits. | M7 |

M1–M3 are built, and M0 is done. Fighting an NPC falls back to fleeing: the agent aims `Use` only at characters, although a weapon `Use` on the block an NPC stands on attacks it.

M0 and M4, M6–M12 are specified in [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md), with the game facts and their sources in [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md). M4 there replaces the planner sketched in **Planner** above. Once M6 and M7 land, the playable plan's executor and state machine supersede **Scheduler**, **Reflexes** and **Plan** above, and the one-intent queues in **Real time**; until then those sections describe the shipped agent. The call budget is unchanged: one request per character per tick, burst 3.

## Tests

Standard library `unittest`, no server. They cover what the agent decides, not the wire: pathing, frontier choice, reflex order, and the scheduler's choice of call. Run with `python -m unittest` from `python`.
