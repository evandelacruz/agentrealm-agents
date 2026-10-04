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
| `GET /characters/{id}/minimap` | Every revealed map's size and level entrance marks (`entrances`: `{x, y}` per map). |
| `POST /characters/{id}/tick` `{"intents": [{...}], "snapshot_version": N}` | Replaces the character's queue with an ordered list (`[]` clears it; no `intents` leaves it running). Optional `snapshot_version` is the observation version last applied; the server answers with a delta when it still matches. Returns `queue_id`, `intent_results` since the last call, events by tick, dropped count, the observation, and the clock. |

Auth is `Authorization: Bearer <key>`.

A `Use` target `{"kind": "npc", "npc_id": N}` names the block that NPC stands on when the `Use` runs ([API rules § Use](https://agentrealm.gg/docs/api)). It ships in server release 1.11 (saims B126); **Fight**'s NPC swings depend on it (A45).

**One request per character per tick, with a burst of 3, on every character route.** Reads count. The limiter is a token bucket refilled on the front's tick interval ([Manual §7.4](https://agentrealm.gg/docs/manual#74-rate-limits)). So the agent runs a call budget: each tick it spends its call on the read it most needs or on the tick submit. The round trip carries the observation (B15). This agent reads ground chest contents and its own `health` / `max_health` from it; **Retreat** (A9) uses health with the threat table and directive params, and **Heal** (A10) uses it when hurt and out of combat. Each `Damaged` event also updates a threat table, after the same response's observation is applied: max damage per hit per hostile type, keyed by the source's type code, with an unmeasured default until something is measured (A6). A hit whose source is not among the entities perceived before or after that response, or has no type code, is not recorded. Trap and `occupy` damage is recorded under its own keys but never raises the default for an unmeasured hostile. **Fight** (A23) will add the full win estimate and margins. Terrain and entity reads still compete with intents for the same budget.

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

Movement goes as a paced `Step`, `Wait`×n, … queue along the path, cut at the world's horizon, with no trailing `Wait`s (M6). The waits per step are the ticks per move at `movement_speed`, rounded up. The next queue opens with the `Wait`s still owed since the last applied `Step`, counted to the latest tick a server response reported, not the windows counted locally since, so it never draws `movement_cooldown`. Position follows each `Step` result as it arrives, not the submit. A result counts only if it names the `queue_id` our own submit was answered with; a late result for an earlier queue is ignored, never adopted, even when the submit's response named no queue. While that queue is still running, the window sends nothing unless a reflex below fires or a read or event makes the rest of the path wrong (`BlockChanged`, an entity on the path, a terrain read that disagrees); in those cases the next poll replaces the queue with a replanned walk. The queue is dropped when a reflex fires (its intent replaces the queue, and since the dropped queue's later results are no longer read, position is re-read and the path forgotten), when a `Step` is rejected (a rejection discards the rest server-side), when a death clears it, or when its results have not come back a couple of ticks past its length (results that never name our queue must not stall the character; we may have walked unseen, so that drop also re-reads position and forgets the path and the last `Step` tick). A `Step` onto a door replaces the rest of the queue with `[]` on the very next call, before anything else is read, because the `Step`s behind it were planned from the wrong place. Once a rejection or a door has dropped the queue, a server `queue` echoed on that same response does not bring the hold back. A rejection, a door, a death, a firing reflex, or unmatched results send us back to step 1.

`Attacked`, `Damaged`, and `Died` on a queue are always ours: they carry no `subject_id` there.

### Reflexes: every round trip, no model

The first rule that matches picks the intent. They run on every `POST tick`, also while a movement queue is in flight: rules 2–4b drop that queue and send their intent in its place; rule 5 leaves a live queue running.

1. Previous intent rejected → clear the path. A rejected `Step` teaches the map by its code (A14, `navigation/rejection.py`), every lesson keyed by map and cell:

   | Code | What the agent records |
   |---|---|
   | `not_traversable` | Impassable until a `BlockChanged` on that map and cell. |
   | `block_occupied` | Kept off for the next decision, then costs 100 extra for 30 ticks. |
   | `conflict_lost` | Nothing; the next move may retry. |
   | `door_locked` | Impassable, and stored as a locked door under the map's `doors` in the knowledge base, which every character of the world then keeps off. |
   | `over_strength_ceiling` | Impassable until the loadout (armed or worn) changes, and stored under the map's `hunting` in the knowledge base with the zone's ceiling for A27's strength bracket. |
   | `would_strand` | Treated as anything else below. The server refuses the move that would strand us (GAME_NOTES: unequipping the supply that keeps us on water), so the agent is never left stuck; it just tries something else next decision. |
   | anything else | Kept off for the next decision only. |
2. Standing on a block in `avoid_blocks` → step to the nearest safe neighbour.
3. Hostile in range: `on_hostile = "flee"` → step to the neighbour farthest from it. `"fight"` → the **Fight** state (A23), not a reflex here.
4. `pickup = true` and a worthwhile supply underfoot, adjacent, or in a ground chest within reach → `Take`, or `WithdrawFromChest` with that one `supply_id`. With the carried chest full, `Drop` the lowest-valued held supply first, only for a pickup worth more; otherwise skip it (A20).
4a. A worthwhile free supply or known chest supply in sight, `pickup = true`, and a plannable step toward it → the **Loot** state (A20) walks there, after Recover and before Explore. It claims the round only when it sends an intent; otherwise Explore runs.
4b. Our last death dropped a chest on this map, `pickup = true`, and a tile on or next to it is known safe (A7) → the **Recover** state (A11) walks there; on or next to it, `WithdrawFromChest` with only its `chest_id`, which takes everything that fits, until the snapshot shows it empty or gone. `Died` names the chest and where it landed; tick POSTs carry the last applied observation version when we have one, so most round trips get deltas and the chest's `contents` stay in the model without a full snapshot every time.
4c. Directives `goals` name a `travel:*` destination that resolves → the **Travel** state (A27, priority 5: below Heal and Recover, above Explore) walks the first resolvable op of the stack; arriving drops it, an unresolved one is skipped and dropped once a later one is acted on (A27 row).
5. Plan has a next step → walk the path as a paced `Step` queue (see **Scheduler**).
6. Otherwise → nothing.

"Hostile" and "in range" read from settings: the API serves no hostile's reach and no other character's health.

### Plan: goal and path

A goal plus a path over the M7 cost grid (A12–A13, `navigation/planner.py`). Step costs: known walkable 1; fog 2, so unseen ground is assumed open and paths may run through it; fire and lava 1 plus their `occupy_damage` from terrain reads, or plus 100 when no read has named it; an NPC or character standing there plus 50 (high but finite: they move); a cell a `block_occupied` rejection named in the last 30 ticks plus 100; each hostile in `policy.hostile` plus 30 minus 5 per block of distance, out to 6 blocks. Known blocked tiles, void, tiles a rejection made impassable (reflex 1), and break-nominated cells (until M9) are impassable, and a door is entered only as the goal, since stepping onto one warps. A* runs over the known extent plus start and goal with a one-tile fog ring, so an unreachable goal returns no path instead of searching fog forever. It expands at most 400 cells per replan (A13); when it finds the goal or proves it unreachable, that is the plan. When the budget runs out first, the replan plans only inside the perception window, with the same budget: a goal in sight gets the path to it, or the best partial path toward it when the window does not connect. A goal out of sight also gets a coarse corridor search over 16×16 cache tiles, at most 48 tiles expanded per replan. It runs backward from the goal and is kept per plan (`goto` on its own map, the death chest) in `Memory.corridors` across replans until the goal or map changes or a step is rejected, so each replan carries on where the last stopped and reads the corridor from the cache tile we stand in. A cache tile costs 16 times its mean passable step over the passable share, priced by the same grid (fog, known blocks, hazards, avoided, rejected and costly cells, occupants, hostiles); a finished corridor is re-priced every replan and searched again when a tile on it got dearer or impassable. The window search keeps to the corridor's tiles and those beside them, and ends on the cell with the best cost so far plus twice the estimated distance left along the corridor; until the corridor is finished it heads straight for the goal. The executor walks the known prefix and replans when it runs out. `explore`, `doors`, and the door-graph floods of a cross-map `goto` (A26) pick among known tiles with the unbudgeted search of A12. The next step must be a seen walkable or door tile, and the queued walk stops before the first cell that is occupied or otherwise not open now, so it never Steps onto an NPC the grid priced at 50. A goal whose path starts on an unseen tile is skipped like an unreachable one, so the next goal in the list gets the move; with none left, the agent sends nothing until terrain reads catch up. `avoid_blocks` are impassable except when standing on one with no safe step off: then they cost 100 extra per step, so the plan crosses as few as it can. Movement is Chebyshev: diagonals cost the same as straight steps.

| Goal | Target |
|---|---|
| `explore` | Nearest frontier: a known walkable tile next to an unknown one. |
| `doors` | Nearest known door (`framed_door`, `rock_entry`) whose warp is not yet recorded, else the nearest door. Stepping onto one warps. |
| `goto` | A fixed `(x, y)`, on `policy.goto_map` when set (A26), else the current map. |
| `hold` | Stay. |
| `wander` | A random walkable neighbour. |

Goals are tried in order; the first with a reachable target wins. Re-plan when a step is rejected, when the map changes, or when a terrain read disagrees with the path.

**Door graph (A26, `navigation/door_graph.py`).** A `goto` on another map, or one not reachable on this map, is a Dijkstra search over the door graph: nodes are the start, the goal, every known door and every warp landing; walking edges come from one cost-grid flood per node, and a door with a recorded warp has a zero-cost edge to its landing. A door reached on foot is left only through its warp, so one whose warp is unknown is a dead end; the start walks out even when it stands on a door. The agent walks the first leg; after the warp, it replans from the landing. With no known route the goal yields like any unreachable one. Warps are learned by observation: a `Step` onto a door sets `warp_from`, and the next position read records where it landed, unless it still stands on the door (no self-loops). A refused Step, a death, or a not-on-map error drops the pending warp. Knowledge base shape, under `maps["<map_id>"]` (`knowledge_maps.py`): `terrain` is `{"x,y": block_type}`, merged per terrain read's window and once more from the world model at exit; `doors` is a list of `{x, y, block_type}` sorted by cell, plus `to_map_id`, `to_x`, `to_y` once a warp is recorded, and `locked: true` once a Step onto it was refused `door_locked` (A14).

**Stuck detection (A15, `navigation/stuck.py`).** Every path to a target on the current map is an attempt keyed by goal, map and target cell; Explore (`explore`, `doors`, `goto`, and the plan's `explore_area` and `travel` ops), Travel (`travel:*`), Recover (the death chest) and Level (`level:door`, `level:frontier`) all run through it. Progress is the remaining length of the planned path plus the straight-line rest to the target, read when the state plans or keeps a path; an applied Step only bumps counters, so no search runs per move. The attempt is stuck when that measure has not fallen in 20 applied moves or 300 ticks, when its last 8 cells hold 3 or fewer distinct ones, when 3 of its Steps in a row were rejected (an applied Step resets the run; another goal's Steps do not count), or when the planner finds no path. A route whose first step is taken by an occupant is not "no path": the attempt sends nothing and waits out its window. Escalation follows PLAYABLE_AGENT_PLAN Navigation §4, each level with a fresh window and entered only when the one before failed: step 1 replans with fog at 8 instead of 2 (one that finds no path fails at once); step 3 walks up to 40 moves toward the reachable frontier cell nearest the target, found by a breadth-first search over at most 400 seen open cells, which keeps the route along the obstacle (it stands in for the spec's left-hand wall follow and cannot circle); a plan shorter than any seen before ends the reveal and is walked, and failing again gives up. Step 5 backs the target off for 300 ticks doubled on each give-up, drops its path, and appends a `stuck` signal (goal, reason it first got stuck, why each level failed, explored outline, blocking block types) to `Memory.nav_stuck.stuck_signals`, which keeps the last 16. Nothing reads the signals until the strategist's triggers (A35, M4). A given-up frontier cell is skipped by `explore`, `explore_area` and Level, and a given-up door by `doors` and Level, until its backoff ends; a given-up `goto`, door, Travel destination or chest yields the round to the next goal. Steps 2 and 4 are M9 (A28, A26). Not tracked yet: a leg toward a door to another map, so a cross-map `goto` or Travel escalates only once it stands on the destination's map.

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

A separate runtime file, `characters/<name>.directives.toml`, is re-read whenever it changes (A8). It carries survival `params` (defaults and ranges in [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md)), a hard `never_attack` list enforced in the reflexes and on tick submit, and optional strategist fields (`goals`, `instructions`). On reload, changed `goals` rebuild the goal stack from the top and drop the current path, so the new top op replans at once; unchanged goals keep the stack's progress and the current path, and only reset its params to the file's values. Path ownership follows the plan head: **Explore** keeps walking a path only while it was set for the stack's current top op (same op and target), so a head change, even to another op of the same kind such as a second `travel` point, replans. With no valid `goals`, the built-in plan mirrors `policy.goals` one for one (A34, `plan.py`); a `goals` list where every entry is invalid logs that and falls back to it. Today goals drive only the ops the shipped states can run: `explore_area`, `travel` (to `entrance`, `town` or `point`) and `wait`. Any other op, and `travel` to `hunting_ground` or `shop`, is dropped with a log line when it reaches the top of the stack, and an op that finds no path for 30 seconds is dropped too; meanwhile `policy.goals` get the move, and a path one of them sets is kept until it goes stale instead of being replanned every window. Directives `travel:*` entries are not stack ops: **Travel** (A27) reads them straight from `goals` and keeps its own queue, so the stack's `travel` ops come only from the built-in plan or the strategist. **Gather** (A22) reads a `gather_gems` goal straight from directives `goals`, so dropping it from the stack does not stop it. `wait` seconds are converted at the world's `tick_rate_hz`. `never_attack` and the survival `params` **Retreat** reads (`retreat_hits`, `risk`, `lives_floor`, A9) change behavior today; `fight_margin` feeds only the win estimate **Fight** (A23) will gate on. The plan also keeps a copy of the params (`Plan.params`) for the strategist. A file that fails to read or parse keeps the last good directives, or the defaults on first load; deleting the file restores the defaults.

## CLI

```
python -m agentrealm_agent create characters/wren.toml
python -m agentrealm_agent run characters/wren.toml [characters/kit.toml ...]
python -m agentrealm_agent status characters/wren.toml
python -m agentrealm_agent metrics characters/wren.toml
python -m agentrealm_agent compare-metrics baseline.json candidate.json
```

Environment: `AGENTREALM_BASE_URL` (default `http://localhost:8080`, a local stack; the public API is `https://api.agentrealm.gg`, where lives are permanent), `AGENTREALM_API_KEY`. After `make up` in the game repo, run [`scripts/seed_local_stack.py`](scripts/seed_local_stack.py) with `AGENTREALM_STACK_DIR` pointing at that compose project so the front tier logs yield a verification token; it mints the first key and writes `export` lines to `python/.state/local.env` (mode 0600, git-ignored). Re-running reuses that key. `--probe` also waits until sandbox `create` succeeds, at the cost of a character that holds one of the account's two sandbox slots for 24 hours (Manual §13).

`run` drives every listed character, one thread each. Each character logs one line per window to stdout (tick, position, call made, intent, the result of the last one, events) and a JSONL trace to `.state/<name>.trace.jsonl`, so a death can be read back as a decision. `metrics` reads the last run from that trace (the trace is appended to; each run starts with a `world` record) and prints levels cleared, deaths, kills, gems, time per level, and how many lines it could not parse. Time per level runs from leaving the overworld (the town's map) to the level's `level_clear_ceremony`, across every map of the level; a run that starts inside a level has no time for it (A41). `compare-metrics` subtracts a baseline from a candidate and prints the deltas, so a regression shows up as a number (A42). Each side is a trace (`.jsonl`), a metrics snapshot (`.json`), a captured `metrics` line for one character (a capture with several lines is refused), or a character file whose trace is read. A delta is `null` when either side has no value: `gems` never seen, or a level timed in only one run (cleared in one and not the other, or entered mid-level), which would otherwise read as a slowdown or speedup of the whole level time.

## Server gaps that limit the agent

| Gap | Effect on the agent | Where it lands |
|---|---|---|
| Sandbox content loads on the sim's first start; the `default` outfit is seeded | Create works with avatar `default` once the sim has started; before that it answers `world_not_ready`. | B13, B39 |
| No character delete and no read-only sandbox readiness signal | The seed script can confirm the sandbox map is loaded only by creating a probe character, which then holds one of the account's two sandbox slots until it ends, so the probe is opt-in (M5). | None |
| No hostile's reach is served | `hostile_range` is a guess in the character file. | None |
| No NPC's health or damage is served, except a boss's health | Hostile health and damage per type are learned from `NPCDamaged`, `NPCDied` and `Damaged`, with conservative defaults until measured ([`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) Combat). | None |
| No supply's capabilities (cut, chop, smash, burn, blast, light, water) are served on any read | The item table stores none. They come from the manual's per-class rules ([`docs/GAME_NOTES.md`](docs/GAME_NOTES.md) Movement and blocks) or from break results (A28). | None |
| `Use` on an NPC by id ships in server release 1.11 | **Fight** aims every NPC swing by id (A45), so against a server older than 1.11 it cannot land a hit on an NPC. | B126 |
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

**M6: Executor.** Merged on `main`: paced `Step`/`Wait` movement in the runner, `Use` and `Say`/`Broadcast` through `executor/pacing.py` with cooldowns carried across queues (A1), multi-intent queues, the two poll cadences, queue invalidation on reflexes (A2) and on path changes (A43), applying snapshot versions, deltas and health in the world model, and tick POSTs that carry the last applied observation version (A3). The live Olympuff acceptance script [`scripts/smoke_m6_olympuff.py`](scripts/smoke_m6_olympuff.py) runs the done-when (A4); A4 stays open until a live PASS is recorded.

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
| A5 | **State framework.** `State` with `guard`/`act`/`done`, a priority dispatcher replacing `brain.decide`, `Sync`, `Downed`, `Explore`, `Idle`; the `list[Intent]` test seam. A state that claims the round but sends no intent falls through to the next state; that rule lives in the dispatcher (A44), so states do not add their own fallbacks. A state that means to hold the round with nothing to send (Sync, Downed, Idle, a Flee or Heal wait, Recover opening the chest) returns `StateOutcome(..., wait=True)`. | A1, A2 |
| A6 | **Threat table.** Damage per hit per hostile type from `Damaged`; the unmeasured default. | |
| A7 | **Safe-tile discovery.** `get_zone` around the respawn point and along the route, within the call budget. Discovery records safe tiles; **Retreat** (A9) and **Heal** (A10) walk to them, and **Recover** (A11) walks to a death chest only from a known safe tile on or beside it. A failed zone read drops that cell from probing. | |
| A8 | **Runtime directives.** `characters/<name>.directives.toml`, re-read on change; params with ranges and defaults; `never_attack` enforced in the executor. | |
| A9 | **Retreat, Flee and Escape.** `retreat_hits` and the `risk`/`lives_floor` formula; retreat to a known safe tile. **Retreat** fires only from health against a hostile in range (a hit's size comes from what is attacking). **Flee** honors `policy.on_hostile` as before, not the win estimate; the estimate ships here for A23, on the assumptions in GAME_NOTES.md Open questions. | A5, A6, A7, A8 |
| A10 | **Heal.** Food in reach, carried potion, measured safe-zone regeneration, else wait in town and raise `buy`. Ground food is walked to on a cost path or `Take`n; carried food, then a carried potion, is `Arm` + `Use` self. Whether apples and berries heal on pickup or only carried and `Use`d is unmeasured (GAME_NOTES open measurements), so both are tried and their heal amounts are not learned. **Not done here:** the weapon is not re-armed after a drink, so the character stays unarmed until A24. Each `Take` or `Use` of one supply is sent at most 3 times, since a rejection is not read back. Only a measured "yes" for safe-zone regen is saved to the knowledge base; a "no" (200 ticks in a safe zone with no health back) holds for that run. Any wait with no health back for 600 ticks, or no reachable known safe tile, yields to Explore for 300 ticks. The `buy` ops land in `Memory.buy_signals`, which nothing reads until A21. | A5, A7 |
| A11 | **Recover.** Walk to the death chest only when the spot is safe. Needs `pickup = true` and a known safe tile on or beside the chest on this map; reflexes 2–4 still run first, and with no plannable step the state falls back to Explore's goals for that round. Only the destination is checked: the route to it is not, which is A9's. | A5, A7 |
| A12 | **Cost-grid planner.** The cost table (fog, hazards, hostile danger, expiring occupants, break costs inert), walking the known prefix. | |
| A13 | **Two-level search.** Coarse 16×16 corridor search and A* in the perception window, each with a node budget per tick. | A12 |
| A14 | **Rejection learning.** What each rejection code teaches the map (reflex 1 table): impassable, occupant cost for 30 ticks, locked doors and hunting closures in the knowledge base. `would_strand` is treated like any other code (reflex 1 table). | A12, A17 |
| A15 | **Stuck detection and escalation.** Steps 1, 3 and 5, backoff, frontier drop; the navigation fixtures and trace replay tests. | A5, A12, A14 |
| A16 | **M7 acceptance.** M7 done-when. | A4, A9, A10, A11, A13, A15, A44 |
| A44 | **Dispatcher fall-through.** A state whose `guard` holds but whose `act` sends no intent yields the round to the next state, so a guard/act mismatch can never freeze the agent. The dispatcher enforces it once; a test drives each shipped state through that case. The one exception is typed, never keyed on state names or reasons: an outcome with `wait=True` is an intentional wait and keeps the round. Each yielded `"State: reason"` is kept on the outcome's `yielded` list, and in the reason when no state sends anything. | A5 |

**M8: Gear, economy and combat.** Loot (A20) is a partial: hearts and gems first wait on their supply codes (GAME_NOTES open questions).

| ID | Item | Depends on |
|---|---|---|
| A17 | **Per-world knowledge base.** `python/.state/worlds/<world_code>.json`: load, save, sections, shared by the world's characters. Loaded once when `run` starts, saved once at exit; one `run` process per world. | |
| A18 | **Item table.** `items` in the knowledge base, keyed by `supply_subtype_code`. A row holds only served facts. `attack_range` and `gem_price` are positive integers that overwrite the last: `attack_range` from a `Use` rejected `target_out_of_range` (it carries the reach judged by, B100), filed under the subtype armed in that same response's observation (`get_self`'s `attack_range` names no subtype and can trail an `Arm`, so it is not used); `gem_price` from supplies on entity reads and snapshot entities. `weapon_damage` is `{npc_type_code: max observed hit}`: the largest `NPCDamaged` amount this subtype has landed on that NPC type, raised only by a larger hit. A hit counts only when it is the one `NPCDamaged` on the block and tick an applied `Use` resolved, no other character was in sight when the `Use` applied and at the response's observation (`NPCDamaged` reaches everyone who sees the block and a miss emits nothing, so any other possible attacker makes the hit unattributable), and exactly one NPC was seen on that block when the `Use` applied (its type is the key); it is filed under the armed subtype after that response's observation. Damage is rolled from 1 up to a cap the target's defense lowers (docs/GAME_NOTES.md Combat), so the number is a lower bound on the weapon's best hit against that type, not the weapon's damage stat. Not stored: damage saved per worn item (a `Damaged` amount depends on the attacker, and what the same attacker deals with the item off is not known, so no per-item number exists yet; docs/GAME_NOTES.md), and capabilities (not served; Server gaps). | A17 |
| A19 | **Equip.** Score slots, swap when a carried item is better. | A5, A18 |
| A20 | **Loot.** `Take`, `WithdrawFromChest`, `Drop` junk when full; hearts first. Shipped: the Loot state and reflex 4 (walk, `Take`, `WithdrawFromChest` one supply, `Drop` the worst held supply for a better pickup, skip when not worth it), carry space from the snapshot's `inventory` at the sourced 10 slots, `carry_capacity_full` and `not_transferable` learned from rejections, and Recover (A11) at the death chest skips a doomed `WithdrawFromChest` when the pack is full and drops junk only when the chest still beats what is held, then withdraws the best supplies by id rather than the lowest ids. Remaining: hearts and gems first, off until their supply codes are known; dropping stowed supplies and larger chests' capacity, both undocumented (GAME_NOTES open questions). | A5 |
| A21 | **Shop.** Buy in-sight priced supplies the plan wants; restock to `potion_reserve`. Consumes Heal's `buy` ops (`Memory.buy_signals`, A10). | A5, A18 |
| A22 | **Gather.** Gems from grass, bushes and gem piles in safe-ish ground. | A5 |
| A23 | **Fight.** Group-aware win estimate, `never_attack`, retreat queued behind attacks, conservative until measured, never from a safe zone. Gates Fight on A9's estimate (`survival.would_lose`): a fight it says we lose goes to **Flee**, and the "threat outclasses us" **Retreat** trigger lands with it. | A5, A6, A8, A9 |
| A24 | **Healing from food and potions.** `Arm` + `Use` self; heal amounts learned per type; re-`Arm` the weapon after a drink (A10 leaves it unarmed). | A10, A20 |
| A25 | **M8 acceptance.** M8 done-when. | A16, A19, A21, A22, A23, A24 |
| A45 | **Use on an NPC by id.** Fight's swings target `{"kind": "npc", "npc_id": N}` (`states/intents.use_npc`), so the server swings at the block the NPC stands on the tick the `Use` runs (server release 1.11, saims B126; [API rules § Use](https://agentrealm.gg/docs/api)) and a queued swing follows an NPC that moved after the plan. An NPC out of sight, dead or unknown that tick is `target_out_of_range`, which drops the rest of the queue and re-plans like any rejection. The item table matches `NPCDamaged` against the block the NPC was last seen on and files the hit only when the one NPC seen there is the target, so a hit after it moved, or on another NPC on that block, is left unattributed. `never_attack` checks the target NPC's type by id and fails closed: with it set, an npc id not in the entity list drops the swing. No `direction` target is used: the agent never aims a `Use` at a neighbour meant relative to where it will stand. | A23 |

**M9: Navigation and knowledge.**

| ID | Item | Depends on |
|---|---|---|
| A26 | **Door graph and cross-map routing.** Warps recorded in the knowledge base; route over the graph, then A* on each map. | A12, A17 |
| A27 | **Travel.** To entrance marks, town, hunting grounds and shops; strength bracketed by `over_strength_ceiling`. Directives `goals` entries `travel:<to>[:[map_id:]x:y]` form a stack, walked in order with the cost grid and door graph (A26) after reflexes 2–4. An op that does not resolve yet (`shop` before any priced supply is seen, `town` with no town or respawn known, `hunting_ground` with no eligible ceiling) is skipped: dropped once Travel acts on a later op, kept while none resolves, so Explore runs until the knowledge base can resolve it. Arriving drops the op; with no step plannable, Travel falls back to Explore's goals for that round. A shop is a cell where a supply with a `gem_price` was seen: a priced supply "spends those gems when picked up" ([manual §11](https://agentrealm.gg/docs/manual#11-game-rules)). The API names no shops, and a bought-out cell stays listed. Entrance marks come from one `get_minimap` read at startup, so marks on maps revealed later are learned on the next run. The bracket keeps only the lower bound (`over_strength_ceiling` ⇒ strength above that ceiling): a successful entry would not change which grounds are candidates until win estimates (M8) choose between ceilings. A loadout change resets it and reopens the cells it closed. | A5, A26 |
| A28 | **Break and break memory.** Per (block, capability); break costs go live; escalation steps 2 and 4; `Escape` through blocks. | A5, A15, A17 |
| A29 | **M9 acceptance.** M9 done-when. | A25, A27, A28 |

**M10: Curiosity and clues.**

| ID | Item | Depends on |
|---|---|---|
| A30 | **Interest list and Investigate.** `Read`, `Say`, `get_zone`, walk to look; the curiosity budget. Shipped: `Read` of readable cells in sight and `Say` once to NPCs within 25 blocks, from where the agent stands (free, never charged); an applied one is remembered in the knowledge base, a target refused 3 times is dropped for the run; `get_zone` is A7's spare-window probes (respawn ring first, then the path); door and entrance looks walk next to an A27 minimap mark or a door with no recorded warp and no look yet, on the current map only (so no look routes through a door before the cap), read `block_type` and `locked` from terrain (Manual §9.2), and store them with `looked` on the `kb.entrances` row and the map's door entry; `kb.entrances` is keyed `"<map_id>:<x>,<y>"` (old `"x,y"` rows are rekeyed from their `map_id` once, when the knowledge base loads); `needs` is set only to `key` on a locked door (Manual §9.2, §11), since a block never says whether it breaks and "across water" is the route, not the cell; a look with no route, or one that cannot read the cell from beside it, counts as a refusal. Deferred: the curiosity cap (to A32, with the first detour item; walk-to-look detours are not capped yet); looks on other maps (with the cap); scrolls (no sourced way to tell a scroll supply from its code; GAME_NOTES open questions). | A5, A17 |
| A31 | **Odd-block detector.** Nominates blocks for `Break`. | A28 |
| A32 | **Clue capture and no-LLM clue rules.** Text with place and time; direction and capability biases. | A30 |
| A33 | **M10 acceptance.** M10 done-when. | A29, A31, A32 |

**M4: Strategist.** Its directives file is A8.

| ID | Item | Depends on |
|---|---|---|
| A34 | **Plan schema and goal stack.** Op table, field validation, param limits; states consume goals; the built-in plan with no model. Gather (A22) reads `gather_gems[:count]` straight from directives `goals` via `plan_goals.py`. | A5, A8 |
| A35 | **Strategist thread.** Optional LLM dependency, triggers, rate and cost limits, trace logging. | A32, A34 |
| A36 | **M4 acceptance.** M4 done-when. | A33, A35 |

**M11: Levels.**

| ID | Item | Depends on |
|---|---|---|
| A37 | **Level.** Priority 5, below Travel and above Explore. Active when the position read names a positive integer `level` (Manual §5.3); `level` belongs to the map, so a position read or delta that omits it keeps the current map's level, and a new map has none until a read names it; a non-integer `level` is logged and ignored, keeping the last good value. Walks toward the nearest door whose warp is not yet recorded, else the nearest door, else a frontier tile; fight and pickup reflexes still run first (same door preference as the `doors` goal). Exit rule: a `travel:*` directive that resolves outranks Level, so Travel can walk the agent out (to town, say); Level takes over again once that op is reached or while none resolves. With no door or frontier step it sends nothing without `wait`, so dispatch falls through to Travel's or Explore's goals (A44). | A23, A27 |
| A38 | **Boss.** Plan preconditions, the clock, progress from boss `health`. | A37 |
| A39 | **Solve.** `Compose`, keys at doors, `use_block`. Priority 4, after Investigate and before Gather; runs while the stack's top op is `compose` or `use_block` (`pathing.plan_step` leaves those to it). `compose` sends `Compose` with one held fragment per slot (lowest id; a duplicate piece stays in the inventory) once no held fragment reports `missing_slots` and the slots held cover `piece_count`; slots may be 0, and a fragment whose `missing_slots` cannot be read is kept and judged by the slots held. It is done when the whole is held. `use_block` `Arm`s the named supply if it is not armed, walks into its `attack_range` of the target (default 1), and `Use`s the block. Only supplies in hand count: one in the carried chest is not withdrawn. It is done when the target's `block_type` differs from what it was when the op reached the top (a broken block shows its destroyed type, GAME_NOTES Breaking blocks), as a `BlockChanged` or terrain read shows; a tile-name guess never finishes it. An op that makes no progress for `PLAN_STALL_SECONDS` (30 s) is dropped with a log line, as A34 drops ops with no path: a step toward the target is progress; sending nothing (pieces or supply missing, target unreachable) or a `Compose`, `Arm` or `Use` that has not finished the op is not. While it sends nothing, dispatch falls through to the states below (A44). | A34, A37 |
| A40 | **M11 acceptance.** M11 done-when. | A36, A38, A39 |

**M12: Evaluation.**

| ID | Item | Depends on |
|---|---|---|
| A41 | **Run metrics.** Levels cleared, deaths, kills, gems, time per level, from the trace. | A5 |
| A42 | **Comparison across commits.** A regression shows up as a number. | A41 |

**Fight** (A23) swings at NPCs and characters when the win estimate clears `fight_margin`, using `Use` on the NPC by id (the server finds its block on the tick the swing runs, A45), with retreat steps queued behind the attack. Out of weapon reach it steps closer; with no open step closer it lets go and **Flee** runs.

The playable plan's strategist (M4) replaces the planner sketched in **Planner** above. Once M6 and M7 land, the playable plan's executor and state machine supersede **Scheduler**, **Reflexes** and **Plan** above, and the remaining one-intent queues in **Real time**; until then those sections describe the shipped agent. Since A5, `states.dispatch` picks the intents; `brain.decide` stays only as a shim over it that keeps the first intent as a `Decision`, until the runner sends a state's whole queue. The call budget is unchanged: one request per character per tick, burst 3.

## Tests

Standard library `unittest`, no server. They cover what the agent decides, not the wire: pathing, frontier choice, reflex order, and the scheduler's choice of call. Run with `python -m unittest` from `python`.
