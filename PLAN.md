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
| `GET /characters/{id}/zone?map_id&x&y` | Zone at a revealed cell: `safe`, `brightness`, and a hunting ground's `strength_ceiling`. |
| `POST /characters/{id}/tick` `{"intents": [{...}], "snapshot_version": N}` | Replaces the character's queue with an ordered list (`[]` clears it; no `intents` leaves it running). Optional `snapshot_version` is the observation version last applied; the server answers with a delta when it still matches. Returns `queue_id`, `intent_results` since the last call, events by tick, dropped count, the observation, and the clock. |

Auth is `Authorization: Bearer <key>`.

**One request per character per tick, with a burst of 3, on every character route.** Reads count. The limiter is a token bucket refilled on the front's tick interval ([Manual §7.4](https://agentrealm.gg/docs/manual#74-rate-limits)). So the agent runs a call budget: each tick it spends its call on the read it most needs or on the tick submit. The round trip carries the observation (B15). This agent reads ground chest contents and its own `health` / `max_health` from it (tracked, not yet used by any decision). Each `Damaged` event also updates a threat table, after the same response's observation is applied: max damage per hit per hostile type, keyed by the source's type code, with an unmeasured default until something is measured (A6). A hit whose source is not among the entities perceived before or after that response, or has no type code, is not recorded. Trap and `occupy` damage is recorded under its own keys but never raises the default for an unmeasured hostile. Retreat and fight margins will read it in later M7 slices. Terrain and entity reads still compete with intents for the same budget.

The runner paces one call per wall-clock window (`epoch / tick interval`). That is stricter than the bucket requires: safe, but slower than it could be.

## Real time

Worlds run at 10 ticks per second by default. The agent's shape already fits: the planner is the slow loop and writes plans off the tick, and the reflexes are the fast executor. The intent queue (B98) lets one request carry up to four seconds of intents, run one per tick; the agent sends movement as a paced `Step`/`Wait` queue, `Use` and `Say`/`Broadcast` through the attack and speech pacers, and most other intents as a one-intent queue (M6). A reflex that fires while a queue runs replaces it (see **Reflexes**). When a read or event makes the rest of a running path wrong, position is re-read and the next poll replaces the queue with a walk replanned from there, or with an empty queue when no route is left. Only a cell that became blocked after the queue was sent counts, so a blocker the plan could not avoid does not cost a resend every poll (A43). `run` does not send `Sleep` (B45) when it stops, so a stopped character stays standing until auto-sleep takes it off the map. See [Real-time play](https://agentrealm.gg/docs/guides/create-a-character-agent#real-time-play).

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
4. Otherwise → `POST tick` with the chosen intent, or with none, when the cadence below says it is due; else a pending `get_zone` on a revealed cell around a respawn anchor or along the path (A7), or send nothing this window.

`POST tick` runs on two cadences (M6). Urgent, meaning a hostile within 3 blocks, a `Damaged`/`Attacked` not yet re-read, or `Damaged` in the last round trip: every window. Calm: every 4–10 ticks, never later than the intents still queued run out. A paced movement queue opens the gap up to its length; a one-intent queue still brings the next poll a tick later. A window the gap skips sends nothing unless step 4 has a zone probe ready. The reads above outrank it, so a calm gap's spare windows go to stale terrain first, then stale entities, then safe-tile discovery. Each window counts as one tick, so a skipped window still brings the next poll and `entity_refresh` due.

Self is re-read after `Died`, and every 60 windows otherwise.

Movement goes as a paced `Step`, `Wait`×n, … queue along the path, cut at the world's horizon, with no trailing `Wait`s (M6). The waits per step are the ticks per move at `movement_speed`, rounded up. The next queue opens with the `Wait`s still owed since the last applied `Step`, counted to the latest tick we know of, so it never draws `movement_cooldown`. Position follows each `Step` result as it arrives, not the submit. A result counts only if it names the `queue_id` our own submit was answered with; a late result for an earlier queue is ignored, never adopted, even when the submit's response named no queue. While that queue is still running, the window sends nothing unless a reflex below fires or a read or event makes the rest of the path wrong (`BlockChanged`, an entity on the path, a terrain read that disagrees); in those cases the next poll replaces the queue with a replanned walk. The queue is dropped when a reflex fires (its intent replaces the queue, and since the dropped queue's later results are no longer read, position is re-read and the path forgotten), when a `Step` is rejected (a rejection discards the rest server-side), when a death clears it, or when its results have not come back a couple of ticks past its length (results that never name our queue must not stall the character; we may have walked unseen, so that drop also re-reads position and forgets the path and the last `Step` tick). A `Step` onto a door replaces the rest of the queue with `[]` on the very next call, before anything else is read, because the `Step`s behind it were planned from the wrong place. Once a rejection or a door has dropped the queue, a server `queue` echoed on that same response does not bring the hold back. A rejection, a door, a death, a firing reflex, or unmatched results send us back to step 1.

`Attacked`, `Damaged`, and `Died` on a queue are always ours: they carry no `subject_id` there.

### Reflexes: every round trip, no model

The first rule that matches picks the intent. They run on every `POST tick`, also while a movement queue is in flight: rules 2–4b drop that queue and send their intent in its place; rule 5 leaves a live queue running.

1. Previous intent rejected → clear the path. The next decision keeps off that block, in the replan too.
2. Standing on a block in `avoid_blocks` → step to the nearest safe neighbour.
3. Hostile in range: `on_hostile = "flee"` → step to the neighbour farthest from it. `"fight"` → `Use` on it.
4. Supply underfoot or adjacent and `pickup = true` → `Take`.
4b. Our last death dropped a chest on this map and `pickup = true` → walk to it; on or next to it, `WithdrawFromChest` with only its `chest_id`, which takes everything that fits, until the snapshot shows it empty or gone. `Died` names the chest and where it landed; tick POSTs carry the last applied observation version when we have one, so most round trips get deltas and the chest's `contents` stay in the model without a full snapshot every time.
5. Plan has a next step → walk the path as a paced `Step` queue (see **Scheduler**).
6. Otherwise → nothing.

"Hostile" and "in range" read from settings: the API serves no hostile's reach and no other character's health.

### Plan: goal and path

A goal plus an A* path over the M7 cost grid (A12, `navigation/planner.py`). Step costs: known walkable 1; fog 2, so unseen ground is assumed open and paths may run through it; fire and lava 1 plus their `occupy_damage` from terrain reads, or plus 100 when no read has named it; an NPC or character standing there plus 50 (high but finite: they move); each hostile in `policy.hostile` plus 30 minus 5 per block of distance, out to 6 blocks. Known blocked tiles, void, rejected tiles, and break-nominated cells (until M9) are impassable, and a door is entered only as the goal, since stepping onto one warps. The search is boxed to the known tiles plus start and goal with a one-tile fog ring, so an unreachable goal returns no path instead of searching fog forever. The executor walks only the known prefix: the next step must be a seen walkable or door tile, and the queued walk stops before the first cell that is occupied or otherwise not open now, so it never Steps onto an NPC the grid priced at 50. A goal whose path starts on an unseen tile is skipped like an unreachable one, so the next goal in the list gets the move; with none left, the agent sends nothing until terrain reads catch up. `avoid_blocks` are impassable except when standing on one with no safe step off: then they cost 100 extra per step, so the plan crosses as few as it can. Movement is Chebyshev: diagonals cost the same as straight steps.

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

A separate runtime file, `characters/<name>.directives.toml`, is re-read whenever it changes (A8). It carries survival `params` (defaults and ranges in [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md)), a hard `never_attack` list enforced in the reflexes and on tick submit, and optional strategist fields (`goals`, `instructions`) for later milestones. Only `never_attack` changes behavior so far; `params` are parsed and validated for the items that will read them (A9, A23). A file that fails to read or parse keeps the last good directives, or the defaults on first load; deleting the file restores the defaults.

## CLI

```
python -m agentrealm_agent create characters/wren.toml
python -m agentrealm_agent run characters/wren.toml [characters/kit.toml ...]
python -m agentrealm_agent status characters/wren.toml
```

Environment: `AGENTREALM_BASE_URL` (default `http://localhost:8080`, a local stack; the public API is `https://api.agentrealm.gg`, where lives are permanent), `AGENTREALM_API_KEY`. After `make up` in the game repo, run [`scripts/seed_local_stack.py`](scripts/seed_local_stack.py) with `AGENTREALM_STACK_DIR` pointing at that compose project so the front tier logs yield a verification token; it mints the first key and writes `export` lines to `python/.state/local.env` (mode 0600, git-ignored). Re-running reuses that key. `--probe` also waits until sandbox `create` succeeds, at the cost of a character that holds one of the account's two sandbox slots for 24 hours (Manual §13).

`run` drives every listed character, one thread each. Each character logs one line per window to stdout (tick, position, call made, intent, the result of the last one, events) and a JSONL trace to `.state/<name>.trace.jsonl`, so a death can be read back as a decision.

## Server gaps that limit the agent

| Gap | Effect on the agent | Where it lands |
|---|---|---|
| Sandbox content loads on the sim's first start; the `default` outfit is seeded | Create works with avatar `default` once the sim has started; before that it answers `world_not_ready`. | B13, B39 |
| No character delete and no read-only sandbox readiness signal | The seed script can confirm the sandbox map is loaded only by creating a probe character, which then holds one of the account's two sandbox slots until it ends, so the probe is opt-in (M5). | None |
| No hostile's reach is served | `hostile_range` is a guess in the character file. | None |
| No NPC's health or damage is served, except a boss's health | Hostile health and damage per type are learned from `NPCDamaged`, `NPCDied` and `Damaged`, with conservative defaults until measured ([`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) Combat). | None |
| Our own strength is served only on the owner watch sheet, not on a character route | The agent brackets its strength from `over_strength_ceiling` rejections and does not read the watch sheet, which sits outside the character call budget. | None |

## Milestones

This table is the backlog. Each row is one PR-sized item with a stable ID; IDs are never reused. Cite the ID in commits and PR bodies. Per-ID state lives in [`status.json`](status.json), which is not a source of truth: where it disagrees with this file, status is wrong. An item is ready when its state is not `done`, every ID in **Depends on** is `done`, its note does not start with "Waiting on", and no open pull request already covers it.

Items are grouped into milestones (M0–M12). A milestone is a heading, not a work item: it is done when all its items are. The scope and done-when of M4 and M6–M12 are in [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) **Milestones**, with the game facts and their sources in [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md). Each milestone ends with an acceptance item that runs its done-when.

**Built (M0–M3, M5).**

| ID | Item | Depends on |
|---|---|---|
| M0 | **Discovery.** Docs read, hand play through MCP, [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md) written. | |
| M1 | **Client and loop.** HTTP client, call scheduler, wall-clock pacing, 429/503 handling, `create`/`run`/`status`, `idle` and `wander`. | |
| M2 | **World model and pathing.** Tile and entity cache per map, local position tracking, A*, `explore`, `doors`, `goto`. | M1 |
| M3 | **Reflexes and scripted characters.** The reflex list, the full character file, the trace. | M2 |
| M5 | **Local seed.** A script that gives the local stack an account, a key, and a playable sandbox map, so `create` works end to end. The `default` outfit is already seeded by migration 00023. The agent's default base URL is the local stack. | M1 |

**M6: Executor.** Merged on `main`: paced `Step`/`Wait` movement in the runner, `Use` and `Say`/`Broadcast` through `executor/pacing.py` with cooldowns carried across queues (A1), multi-intent queues, the two poll cadences, queue invalidation on reflexes (A2) and on path changes (A43), applying snapshot versions, deltas and health in the world model, tick POSTs that carry the last applied observation version (A3), and the live Olympuff acceptance script [`scripts/smoke_m6_olympuff.py`](scripts/smoke_m6_olympuff.py) (A4).

| ID | Item | Depends on |
|---|---|---|
| A1 | **Attack and speech pacing in the runner.** The runner sends `Use` and `Say` through `executor/pacing.py`, carrying cooldowns across queues. | |
| A2 | **Reflexes drop the held queue.** An alarm, hostile or hazard reflex replaces the held server queue on the next round trip. | |
| A43 | **Re-send on a path change.** A read or event that makes the rest of the held path wrong (`BlockChanged`, an entity on the path) replaces the queue. | |
| A3 | **Send `snapshot_version`.** Tick POSTs carry the last applied version so the server answers with deltas. | |
| A4 | **M6 acceptance.** Live smoke test against Olympuff: M6 done-when. | A1, A2, A3, A43 |

**M7: State machine and survival.**

| ID | Item | Depends on |
|---|---|---|
| A5 | **State framework.** `State` with `guard`/`act`/`done`, a priority dispatcher replacing `brain.decide`, `Sync`, `Downed`, `Explore`, `Idle`; the `list[Intent]` test seam. | A1, A2 |
| A6 | **Threat table.** Damage per hit per hostile type from `Damaged`; the unmeasured default. | |
| A7 | **Safe-tile discovery.** `get_zone` around the respawn point and along the route, within the call budget. Discovery only: it records safe tiles; acting on them is A9–A11. A failed zone read drops that cell from probing. | |
| A8 | **Runtime directives.** `characters/<name>.directives.toml`, re-read on change; params with ranges and defaults; `never_attack` enforced in the executor. | |
| A9 | **Retreat, Flee and Escape.** `retreat_hits` and the `risk`/`lives_floor` formula; retreat to a known safe tile. | A5, A6, A7, A8 |
| A10 | **Heal.** Food in reach, carried potion, measured safe-zone regeneration, else wait in town and raise `buy`. | A5, A7 |
| A11 | **Recover.** Walk to the death chest only when the spot is safe. | A5, A7 |
| A12 | **Cost-grid planner.** The cost table (fog, hazards, hostile danger, expiring occupants, break costs inert), walking the known prefix. | |
| A13 | **Two-level search.** Coarse 16×16 corridor search and A* in the perception window, each with a node budget per tick. | A12 |
| A14 | **Rejection learning.** What each rejection code teaches the map. | A12 |
| A15 | **Stuck detection and escalation.** Steps 1, 3 and 5, backoff, frontier drop; the navigation fixtures and trace replay tests. | A5, A12, A14 |
| A16 | **M7 acceptance.** M7 done-when. | A4, A9, A10, A11, A13, A15 |

**M8: Gear, economy and combat.**

| ID | Item | Depends on |
|---|---|---|
| A17 | **Per-world knowledge base.** `python/.state/worlds/<world_code>.json`: load, save, sections, shared by the world's characters. Loaded once when `run` starts, saved once at exit; one `run` process per world. | |
| A18 | **Item table.** Keyed by `supply_subtype_code`, filled from `Arm`, `Wear`, prices seen and capabilities. | A17 |
| A19 | **Equip.** Score slots, swap when a carried item is better. | A5, A18 |
| A20 | **Loot.** `Take`, `WithdrawFromChest`, `Drop` junk when full; hearts first. | A5 |
| A21 | **Shop.** Buy in-sight priced supplies the plan wants; restock to `potion_reserve`. | A5, A18 |
| A22 | **Gather.** Gems from grass, bushes and gem piles in safe-ish ground. | A5 |
| A23 | **Fight.** Group-aware win estimate, `never_attack`, retreat queued behind attacks, conservative until measured, never from a safe zone. | A5, A6, A8, A9 |
| A24 | **Healing from food and potions.** `Arm` + `Use` self; heal amounts learned per type. | A10, A20 |
| A25 | **M8 acceptance.** M8 done-when. | A16, A19, A21, A22, A23, A24 |

**M9: Navigation and knowledge.**

| ID | Item | Depends on |
|---|---|---|
| A26 | **Door graph and cross-map routing.** Warps recorded in the knowledge base; route over the graph, then A* on each map. | A12, A17 |
| A27 | **Travel.** To entrance marks, town, hunting grounds and shops; strength bracketed by `over_strength_ceiling`. | A5, A26 |
| A28 | **Break and break memory.** Per (block, capability); break costs go live; escalation steps 2 and 4; `Escape` through blocks. | A5, A15, A17 |
| A29 | **M9 acceptance.** M9 done-when. | A25, A27, A28 |

**M10: Curiosity and clues.**

| ID | Item | Depends on |
|---|---|---|
| A30 | **Interest list and Investigate.** `Read`, `Say`, `get_zone`, walk to look; the curiosity budget. | A5, A17 |
| A31 | **Odd-block detector.** Nominates blocks for `Break`. | A28 |
| A32 | **Clue capture and no-LLM clue rules.** Text with place and time; direction and capability biases. | A30 |
| A33 | **M10 acceptance.** M10 done-when. | A29, A31, A32 |

**M4: Strategist.** Its directives file is A8.

| ID | Item | Depends on |
|---|---|---|
| A34 | **Plan schema and goal stack.** Op table, field validation, param limits; states consume goals; the built-in plan with no model. | A5, A8 |
| A35 | **Strategist thread.** Optional LLM dependency, triggers, rate and cost limits, trace logging. | A32, A34 |
| A36 | **M4 acceptance.** M4 done-when. | A33, A35 |

**M11: Levels.**

| ID | Item | Depends on |
|---|---|---|
| A37 | **Level.** Walk rooms toward unexplored doors. | A23, A27 |
| A38 | **Boss.** Plan preconditions, the clock, progress from boss `health`. | A37 |
| A39 | **Solve.** `Compose`, keys at doors, `use_block`. | A34, A37 |
| A40 | **M11 acceptance.** M11 done-when. | A36, A38, A39 |

**M12: Evaluation.**

| ID | Item | Depends on |
|---|---|---|
| A41 | **Run metrics.** Levels cleared, deaths, kills, gems, time per level, from the trace. | A5 |
| A42 | **Comparison across commits.** A regression shows up as a number. | A41 |

Today the agent falls back to fleeing from every NPC: it aims `Use` only at characters, although a weapon `Use` on the block an NPC stands on attacks it (A23 adds `Fight`).

The playable plan's strategist (M4) replaces the planner sketched in **Planner** above. Once M6 and M7 land, the playable plan's executor and state machine supersede **Scheduler**, **Reflexes** and **Plan** above, and the remaining one-intent queues in **Real time**; until then those sections describe the shipped agent. The call budget is unchanged: one request per character per tick, burst 3.

## Tests

Standard library `unittest`, no server. They cover what the agent decides, not the wire: pathing, frontier choice, reflex order, and the scheduler's choice of call. Run with `python -m unittest` from `python`.
