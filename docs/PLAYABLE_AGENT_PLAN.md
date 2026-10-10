# Plan: an agent that can actually play

Goal: a reference agent that survives, gears up, fights, travels the overworld, solves clues, and clears levels. "Clear all `level_count` levels" is how a world is beaten (`GET /characters/{id}/world`); Olympuff has 8.

The game facts this plan relies on are in [GAME_NOTES.md](GAME_NOTES.md), with sources.

The backlog is [PLAN.md](../PLAN.md) **Milestones**: PR-sized items (A1, A2, …) grouped under the milestones this plan scopes, M0 (discovery, done), M6 to M12, and M4, which this plan redefines as the strategist. PLAN.md says which of its sections this plan supersedes and when.

## Verdict on the approach

A deterministic state machine for play, plus an LLM that plans off the tick, is a good fit for this game. It is the standard hybrid of fast reactive control and slow deliberation, and the API is shaped for it. Intent queues carry up to 4 s of actions, rate limits allow one request per tick, and the game runs at 10 Hz. So no model call can ever sit on the tick path, and none needs to.

Five rules keep it working:

1. **Use priority states, not a flat FSM.** Survival interrupts first, opportunities second, the plan's goal last. A flat graph of every state-to-state transition grows too large to maintain.
2. **The LLM never emits intents.** It emits typed plan operations from a fixed vocabulary (go here, read that, buy this, break that block with this tool). The state machine turns them into moves. Unknown or invalid operations are dropped.
3. **Replays stay deterministic.** Every LLM answer and directive change is written to the trace, so a run can be replayed without the model.
4. **The rules are known; the world must be learned.** M0 answered how levels, combat, items, `Compose` and death work. What each world hides (which entrance needs what, where the secrets are) is told only through in-world text: signs, statues, scrolls, helper lines. Reading that text and acting on it is the strategist's job, and the knowledge base is where it accumulates.
5. **No spoilers in the repo.** The repo is public. World clues, puzzle answers and level details live only in the gitignored per-world knowledge base. Tests use invented worlds.

## What M0 settled

| Unknown | Answer |
|---|---|
| What a level is, how it is cleared | A set of maps behind an entrance door, with one boss room. Clear it by killing the boss in a timed one-on-one fight. The round trip carries `level_clear_ceremony`, max health rises on the first clear, and you are moved outside |
| How levels are entered | Minimap marks show every entrance, unnumbered. An entrance may be open, locked (`locked: true`, a key consumed), hidden behind a breakable block, or across water. Keys can come from other levels' bosses, so levels form a dependency graph |
| What `Compose` is | Fragments compose into a whole once every piece is held. A held fragment describes its whole (`composes_into`, `piece_count`, `missing_slots`). No recipe catalog and no workbench |
| What "build" means | Nothing. No verb places blocks. The world changes only by breaking blocks, which recover, and composing. Dropped from the plan |
| Combat numbers | Published d20 rules. Attack and defense are 0 in Olympuff, so gear is everything. A new character has 10 health. Weak hostiles hit for 1–2 every ~1.5 s, attack anything adjacent, and gang up |
| What NPCs answer to | Helpers return one fixed line to `Say`; shopkeepers may say nothing; hostiles never reply. Lines can be riddles |
| Signs and scrolls | `readable: true` cells (statues included) and scroll supplies; `Read` returns the text, one per tick, no speech cost |
| Breaking blocks | Each block falls only to its own capability (cut, chop, smash, burn, blast) and never says which. The answer is per block, so it must be remembered |
| Request budget and horizon | 1 request/tick, burst 3; queue horizon 40 at 10 Hz. Snapshot deltas only against the current or previous version, so infrequent polls get complete snapshots |

Open measurements are listed at the end of GAME_NOTES.md. Each is gathered by the agent's own logging, not by more hand play.

## What the API offers that the agent ignores today

| API surface | Used today | Needed for |
|---|---|---|
| `Step` + `Wait` pacing (base 2.5 blocks/s means a step every 4 ticks) | Yes for movement: the path goes as a `Step`, `Wait`×n queue cut at the horizon, the next queue carries the waits still owed, and a rejection clears the path. Live Olympuff acceptance (A4) recorded 200+ applied Steps with no `movement_cooldown`, calm tick POSTs at 22% of calm windows, and zero API errors (`docs/acceptance/m6_olympuff_PASS.transcript`) | Moving at a steady pace without spending a request per step |
| Multi-intent queues | Movement, and `Use`/`Say`/`Broadcast` behind the `Wait`s their cooldown still owes; every other intent is sent alone; a reflex that fires replaces a running queue | Freeing the request budget; queuing a retreat with an attack |
| Snapshot deltas (`snapshot_version`), health in the observation | Health and max health tracked from observations; tick POSTs send the last applied version so the server can answer with deltas; **Retreat** (A9) reads health with the threat table; **Heal** (A10) uses them when hurt and out of combat; entity layer still from separate reads | Perception, retreat |
| `Arm`, `Wear`, `Remove`, `Drop`, `attack_range` | No | Gear |
| `Use` on a block or an NPC by id (attacks the NPC, or breaks the block) | No: flees from every NPC | Fighting, opening the way |
| Priced supplies (`gem_price`) | No | Buying gear, potions, tools |
| `Read` (signs, statues, scrolls), `Say` to an NPC | No | Clues |
| `Compose`, fragment slots | No | Composite keys |
| `locked` doors, traps and `Disarm`, `occupy_damage` | No | Level obstacles |
| Zones: `safe`, hunting-ground strength ceiling | No | Where to rest, where to hunt |
| Minimap: level entrance marks | No | Overworld travel |
| `Sleep`, respawn delay, lives | Partly | Ending a session, death handling |

## Architecture

```
            ┌──────────────── strategist (AI planner, async) ───────────────┐
 directives │ triggers: clue read, NPC spoke, new level, stuck, goal done, │
 (hot file) ┤ death. In: state summary + knowledge. Out: plan operations. │
            └──────────────────────────────┬────────────────────────────────┘
                                           ▼ validated plan (goal stack + parameters)
  knowledge base ◄──── world model ◄──── perception (tick deltas, rare terrain reads)
  (per world, .state/, gitignored)
                            │
                            ▼
                    state machine  ── every round trip: pick state by priority, state.act()
                            │
                            ▼
                    executor ── builds paced intent queues, sends one round trip,
                                re-sends only when the queue is invalidated
```

### Executor (M6; replaces the M1 one-intent loop)

- **Paced queues.** Paths become `Step`, `Wait`×n, `Step`, … queues. The number of waits comes from `movement_speed`. Attacks are paced by the weapon cooldown (10 ticks by default), and block breaking spends the same attack accumulator. Speech is paced at 10 ticks. The queue is cut at the world's horizon.
- **Re-send only when the queue goes wrong.** That means a result is rejected, a delta changes something on the path, or the state changes. A rejection discards the rest of the queue, so a path is rebuilt from the current position, not resent.
- **Two cadences.** While calm, poll every 4–10 ticks and spend spare tokens on terrain reads. While a hostile is within 3 blocks or health is dropping, poll every tick.
- **Snapshots.** Deltas come only against the previous version, so the slow cadence gets complete snapshots. The world model applies both shapes.
- **Attack queues carry their own escape.** An attack queue ends with the retreat steps, or is no longer than the next poll. A slow poll must never leave the character swinging after the fight turned.
- **Terrain reads.** With perception 25 the terrain window is 51×51, so terrain is read only on a map change or after moving about half the window.
- **Deltas feed the model.** Health, inventory and entity changes are folded into the world model from them.

Shipped so far: the runner sends movement as paced `Step`/`Wait` queues (`executor/movement.py`), cut at the world's horizon, and polls on the two cadences (`poll_cadence.py`); the calm gap never outlasts the intents still queued, and a reflex that fires (hazard, hostile, supply, chest) replaces the running queue, and a read or event that makes the rest of the path wrong replaces it with a replanned walk after re-reading position (A43). Tick POSTs include the last applied observation version so responses can be deltas. `python/agentrealm_agent/executor/pacing.py` paces attack and speech queues and builds attack queues with their retreat, cut at the world's horizon (`queue_horizon_intents`) and the next poll. The attack and speech accumulators are paced separately in the runner; there is no mixed-queue helper yet.

### State machine

**Superseded in part by A61 (2026-10-06): AI plans, state machine executes** (PLAN.md *Architecture*). The table below keeps the states and their order, but "Enters when" now holds only for the reflexes (Sync, Downed, Escape, Retreat, Heal, Fight, Flee, Recover, and a supply in reach). Every other state is an executor that runs only for the plan's top op; with no op, Explore runs the safe default. Curiosity no longer starts anything on its own: `read`, `say` and `break_block` come from the planner.

States are checked in priority order once per round trip: after each `POST tick` response is folded into the world model, before the next request. The first whose guard holds runs `act`, which returns the queue for the ticks until the next poll. Between round trips the server runs that queue one intent per tick, and nothing runs client-side; a state that must react within a tick (a hostile closing, health dropping) does so by switching the executor to its every-tick cadence. Each state has entry and exit conditions with hysteresis so it does not flip back and forth.

| Priority | State | Enters when | Does |
|---|---|---|---|
| 0 | `Sync` | Unplaced, position unknown, after a warp | Reads self and position, waits for placement |
| 0 | `Downed` | `Died` | Waits for `Respawned`, then queues `Recover` |
| 1 | `Escape` | Standing on damage, or trapped | Steps off; crosses as little hazard as possible |
| 1 | `Retreat` | The next `retreat_hits` expected hits could kill (per-type damage from the threat table), or the threat outclasses us while it is coming for us (hit us recently, in reach, approaching, or a boss) | Goes to the nearest known safe tile a path reaches, else the town cell, then `Heal` (see Health and lives) |
| 1 | `Heal` | Hurt and no hostile in range, or health too low for the next goal | Food in reach: walk onto it or `Take` it; food eaten on pickup heals there, carried food is then `Arm` + `Use` self. Else a carried potion: `Arm` + `Use` self. After each drink, re-`Arm` the weapon it swapped out, even at full health or with a hostile in range. Else, only if safe-zone regeneration has been measured, rests in a safe zone. Else goes to town and waits in the safe zone for the next goal to need less, raising a `buy` for potions (`Shop`, M8) |
| 2 | `Fight` | A hostile is in range, it is not of a kind in `never_attack`, and the win estimate clears the margin, counting every hostile within 2 blocks of it | Closes to `attack_range`, `Use` on the NPC by id (its block on the tick the swing runs), with the retreat queued behind |
| 2 | `Flee` | A hostile is in range and we would lose | Opens distance toward safety; safe zones stop all damage |
| 3 | `Recover` | Our death chest is on a reachable map | Walks next to it (a safe tile next to it is enough), `WithdrawFromChest` |
| 3 | `Equip` | Carrying something better than what is worn or armed | `Arm`, `Wear`, `Remove`; armor scored by damage it would have saved |
| 3 | `Loot` | A worthwhile free supply or chest is near enough | Walks, `Take` or `WithdrawFromChest`, `Drop` junk when full |
| 3 | `Shop` | The plan wants an item that is in sight with a `gem_price` we can pay | Walks up beside it and `Take`s it. Out of sight, walks to the nearest known shop first, else to town to look for one (A71) |
| 4 | `Investigate` | The interest list has an item within the curiosity budget (see Curiosity) | `Read`, `Say`, `get_zone`, walk to look; stores text as a clue |
| 4 | `Break` | The plan names a block to open, or the odd-block detector scores a nearby block high enough | Arms a capability not yet tried on that block, `Use` on the block, records the result per (block, capability) |
| 4 | `Solve` | The plan holds an action to try (compose, a key at a door, a tool at a block) | Carries out the plan operation and checks the result |
| 5 | `Gather` | The plan needs gems | Cuts grass (never bushes, which drop berries) on known ground off hazards with no hostile near, field cells before safe ones, learning ground where cuts have no effect, and visits gem piles; heads out of safe ground when nothing is left to cut |
| 5 | `Level` | Inside a level | Walks the rooms toward the unexplored doors, using `Fight`/`Break`/`Investigate` as they apply |
| 5 | `Boss` | At a boss door with the plan's preconditions met | Enters, fights within the clock; retreats out only if possible, else commits |
| 5 | `Travel` | The plan names a destination (entrance, town, hunting ground, shop) | Cost-grid planner through fog, door graph across maps, stuck detection and escalation (see Navigation) |
| 5 | `Explore` | Nothing else | Frontier exploration, unvisited entrance marks first; frontiers that turn out unreachable are dropped with a backoff |
| 6 | `Idle` / `Sleep` | Stopping, or the plan says wait | Sends nothing, or `Sleep` at shutdown (not allowed inside a level) |

Every state is a small class with `guard(world, plan) -> bool`, `act(world, plan) -> list[Intent] | None` and `done(world) -> bool`. `act` returns the whole queue to send, in order, or `None` to send nothing. That is the M7 test seam: a world model and plan in, a `list[Intent]` out. It replaces today's `brain.decide(...) -> Decision`, which carries at most one intent, so new tests are not written in that shape.

### Navigation and getting unstuck

Paths are never straight lines. Bushes, trees, water, walls, fences, NPCs and other characters are in the way, and most of the map starts as fog. Today's `world.path` is A* over tiles already seen. It returns nothing when the goal is in fog or walled off, and it keeps off a rejected tile for only one decision. So the agent can go round obstacles it has seen, but it can't head for somewhere it hasn't seen, and nothing notices that it has stopped making progress. The fix comes in four parts.

**1. One planner over a cost grid, not a walkable/blocked map.**

| Cell | Cost |
|---|---|
| Known walkable | 1 |
| Fog (never seen) | 2. Assumed open, so the agent can aim at an entrance 300 blocks into the fog |
| An obstacle nominated for breaking (named by the plan, or scored by the odd-block detector) with a capability we hold that break memory has not marked as failed on it | Break time plus 1, plus the gem price of a consumable tool if that is the capability, so a nominated bush in a hedge line is a door, not a wall. Applied only at stuck step 2 or while `Break` walks to its target (A28): otherwise the cell is impassable |
| `fire`, `lava` | 1 plus a cost per point of `occupy_damage`; entered only when there is no other way |
| Near a hostile | A danger cost that falls off with distance, so routes keep away from hostiles |
| NPC or character standing there | High but finite, and it expires: they move |
| Known blocked, including every obstacle not nominated (a block never says whether it breaks or what breaks it), or marked unreachable | Impassable |

The numbers in `navigation/planner.py` (A12), and why:
- **Fog 2.** Twice a known step: the agent still aims into fog, but prefers ground it has seen when the detour is short.
- **Occupant 50.** Worth a long detour, yet finite: people move, so a corridor with someone in it is a last resort, not a wall.
- **Hostile danger 30, falling 5 per block, gone at radius 6.** A step next to a hostile is worth about a 25-block detour, and the cost reaches 0 at 6 blocks, so it bends routes near hostiles without repricing the whole map.
- **Unnamed hazard 100 (`COSTLY_STEP`).** Fire or lava whose `occupy_damage` no read has named costs as much as a `costly` escape tile: assume the worst until a read names the damage, then charge 1 per point.

- **Walk only the part of the path we have seen.** The executor walks the known prefix and replans when terrain reads reveal what lies ahead, or a step is rejected. Fog optimism is corrected by looking.
- **Long trips are two-level.** A coarse search over 16×16-block squares (the API's cache-tile size, API Reads) picks the corridor; A* inside the perception window picks the steps. Each search has a node budget per tick, so a long route never stalls a tick. The budgets are 48 cache tiles for the corridor and 400 cells for each window search (`COARSE_NODE_BUDGET`, `FINE_NODE_BUDGET`). The corridor search is kept per plan (`goto`, the death chest) in `Memory.corridors` and resumed each replan; it starts over when that plan's goal or map changes, and every corridor search is dropped when a step is rejected. A window search that cannot reach the goal ends where the walk can go on, never in a pocket it saw all round, and on a corridor it learns: each cell it searched is priced at what the way on from it costs, so the next decision walks out of a dead end instead of standing in it (A13, free-play run 6).

**2. Rejections teach the map, by code.**

| Rejection | What the agent records |
|---|---|
| `not_traversable` | That cell is blocked until a `BlockChanged` says otherwise |
| `block_occupied` | Cost on that cell for a few seconds; wait one move, then route round |
| `conflict_lost` | Retry next move |
| `door_locked` | Door needs a key; recorded in the knowledge base, not retried |
| `over_strength_ceiling` | Zone closed to us at this strength |
| `would_strand` | Nothing beyond the one-decision wait any other code gets; the server refuses the move that would strand us |

An opening we cut or burned is open only until it grows back (about 60 s for a bush). It is planned through with that deadline, never as permanent.

**3. Stuck detection.** Progress is the remaining path cost to the goal, measured each move. The agent is stuck when any of these hold:
- the remaining cost hasn't fallen in 20 moves or 30 s;
- the same few cells keep being revisited (oscillation);
- 3 moves in a row are rejected;
- the planner finds no path, even with fog assumed open.

**4. Escalation, in order, each step only if the one before fails:**
1. Replan with the learned blocks, and with fog optimism lowered so known ground is preferred.
2. Break through (M9): if a nominated obstacle lies on the best blocked route and we hold a capability not yet tried on it, `Break` it. A failed try is recorded per (block, capability); that pair is never tried again, but another capability may be.
3. Reveal: explore the frontier cells nearest the goal, following the wall of the obstacle (left-hand rule) for a bounded number of moves, to uncover a way round.
4. Change the means: if the goal is enclosed on this map (water, cliffs, a locked door), record what seems to be needed (a raft, a key, a door from another map) and route through the door graph if one is known.
5. Give up for now: mark the goal unreachable with an exponential backoff, pick the next goal, and raise the strategist's `stuck` trigger. The trigger carries the goal, the explored outline and the blocking cell types. The strategist may answer with a tool to buy or a different route.

**Cross-map routing.** Doors are edges of a graph. Each warp records where it landed, unvisited doors are exploration targets, and a route is a search over the door graph, then A* on each map.

**Escape.** If regrowth or a crowd closes the agent in, `Escape` tries the capabilities it holds that break memory has not marked as failed on the enclosing blocks, or waits for the blocker to move. Waiting inside a safe zone costs nothing.

**Tests.** Fixtures with:
- a U-shaped trap: a local minimum that greedy moves fall into;
- a maze;
- a goal behind a hedge line, with and without the tool;
- a goal enclosed by water;
- an NPC parked in a one-wide corridor;
- a corridor that is a dead end once fog is revealed.

Each asserts the goal is reached, or abandoned with the right reason, within a move budget.

### Curiosity

**Since A61 the agent no longer acts on this section by itself**: the interest list, the curiosity budget and the odd-block detector are removed. What follows describes what a planner should look for and turn into `read`, `say` and `break_block` ops.

Progress in this game is hidden behind things a player has to poke at. Helpers drop hints only when spoken to. Signs and statues carry text. Breakable "odd blocks" hide gems, doors and secrets. Nothing announces itself (M §16). So the agent needs a drive to investigate, not just a reflex for whatever it passes.

**Interest list.** Every terrain and entity read adds to it, and the knowledge base remembers what has been done, so nothing is investigated twice.

| Thing | Action | Notes |
|---|---|---|
| Unread `readable` cell (sign, statue, plinth) | `Read` | While it is in sight (perception × zone brightness, plus light, capped at perception), so no walk is needed once seen. Free: no speech cost, one per tick |
| Unread scroll, carried or in sight | `Read` | Free like readable cells once the subtype is known; unseen codes are probed once (PLAN.md A56) |
| NPC id never spoken to | `Say` once | Works from 25 blocks. A helper replies with its line; a hostile stays silent, which also tells us it is not a helper. Spaced 1 s apart |
| Supply type never seen | Walk over or `Take`, if free and safe | Fills the item table |
| Odd block out | Try each capability we hold, cheapest first | Detector below |
| Cell with unusual art, or a statue `facing` differently from its neighbours | Investigate the cells around it | Unsourced guess: the manual says art is a picture only and behaviour comes from `block_type` (M §9.2). Lowest priority until GAME_NOTES open questions confirm it |
| Unvisited door or entrance mark | Walk to it, look | A locked or hidden door gets recorded with what it shows; a locked one needs a key. Any map the knowledge base knows except level interiors (Level, A37), through known door warps when needed, under the curiosity cap (PLAN.md A30) |
| Unknown zone | `get_zone` once | Finds safe zones and hunting grounds |

**Odd-block detector.** This is the manual's motif: one rock in a garden, one bush in a wheat field, one tree in a maze (M §16). A block is odd when:
- its type is rare in its 7×7 neighbourhood (one or two of it);
- most of the surrounding cells are a single different type or art;
- it is breakable in principle: bush, tree, rock, mountain or wall, not water or a door.

A boost comes from a clue that mentions its type or surroundings ("rings hollow", "behind it"). The detector over-reports on purpose. The knowledge base records each try, so a false positive costs a few ticks once.

**Trying a block.**
- Weapons first, since they are free and not used up: a sword cuts and chops, a mallet smashes.
- Then tools, which are used up: matches (5 gems), then bombs (expensive). A tool is spent only if the block scores high or a clue points at it.
- Every outcome is stored per (block, capability): `applied_no_effect`, or what it turned into and what dropped. A pair that failed is never tried again; a pair that worked is reused after the block grows back.
- A capability we lack, on a high-scoring block, becomes a `buy` suggestion for the strategist.

**Budget, so curiosity doesn't get it killed or stalled.**
- Each item scores value × novelty ÷ (distance + danger + consumable cost).
- `Investigate` and `Break` run only when survival and the current goal allow, and get a capped share of time. The directive param `curiosity`, default 0.2, is that share, as a fraction of game ticks: over the last 600 ticks (60 s at 10 Hz), the ticks covered by queues that `Investigate` or `Break` sent may not exceed `curiosity` × 600. Reads and speech from where the agent stands are not counted.
- Both are disabled while a hostile is within `hostile_range` and for the whole of a boss fight.
- Reading and speaking from where the agent stands are nearly free, so they always happen. Detours are what the budget limits.
- Above the cap, items wait for idle moments, or for the strategist to promote one.

**Where the results go.** Every line read or heard goes to the clue list with its place, and fires the strategist's `clue` trigger. The strategist turns clues into goals. With no LLM, simple rules still apply: a clue naming a direction biases `Explore` that way, and a clue naming a capability raises that capability's priority on nearby odd blocks.

### Health and lives

Health is the resource every other decision spends, and lives are the budget behind it. On a live world, at zero lives the character is ended, permanently: `character_ended` on every intent (M §11). The agent tracks both and acts to keep them up.

**What it tracks.** `health` and `max_health` arrive in every round trip's observation while awake (`Heal`, A10, reads them). `lives` is in the snapshot too. Each `Damaged` event is logged with its source, so the agent knows what is hurting it and how fast.

**Lives set how bold it is.** A single risk level, from cautious to bold, follows the lives left. It scales:
- the fight margin;
- the retreat threshold;
- whether it engages a hostile type it hasn't measured;
- whether it enters a level at all.

Below a floor, 3 lives by default, it stops fighting anything but measured weak hostiles and goes no further than the edge of explored ground. The `lives_floor` and `risk` directives set these.

**How `risk` combines with the other params.** The effective risk is `risk × min(1, (lives − lives_floor) / lives_floor)`, floored at 0, so it falls to 0 at the floor. The effective fight margin is `fight_margin × (1.5 − effective risk)`, and the effective retreat threshold is `retreat_hits + round(1 − 2 × effective risk)`, never below 1; at effective risk 0.5 both apply as set. Below effective risk 0.5 it never engages an unmeasured type, and at 0 it does not enter a level.

**Protecting health in the moment:**
- **Never start a fight hurt.** `Fight` needs health above the expected damage of the whole group over the fight, plus a margin. Otherwise heal first.
- **Retreat in time.** The threshold is set in hits, not percent: when the next `retreat_hits` (default 2) hits from what is attacking could kill, it leaves. A hit's size comes from the threat table, learned per hostile type from `Damaged` events (the API serves no hostile's damage). A type not yet measured is assumed to hit as hard as the hardest measured type, and before anything is measured, 2, the most a weak hostile dealt in M0. A hit is filed under its source's type code, resolved against the entities perceived around that round trip; a hit from a source not perceived there, or with no type code, is not recorded rather than filed under its id. Trap and `occupy` damage is kept apart and never sets a hostile's default. The steps away are already queued behind every attack (see Executor).
- **Drink mid-fight** only when retreating is impossible, such as a boss room or being cornered. Swapping in a potion costs a tick, drinking costs another, and re-arming the weapon a third, so the agent compares those three ticks of incoming damage with the heal.
- **Step off damaging ground** at once (`Escape`). Fire and lava are crossed only when the route has no other way.
- **Traps.** Wear goggles when they are owned and the area is trapped. Never walk on a seen armed trap.
- **Never stand around exposed.** `Sleep` in a safe zone when the run ends, since an awake, unattended character keeps taking hits. Idle waits happen in safe zones.

**Healing:**
- **Food first, it's free.** Food lying in sight is picked up (walk onto it or `Take`) when the amount missing is at least what it heals, and is remembered as a source. Some food is eaten on pickup, like Olympuff's golden cap (M §16); carried food is eaten with `Arm` + `Use` self, the same call as a potion (API Use). Which kind each type is, and how much it heals, is learned from the `health` change.
- **Potions are a reserve.** The agent keeps N potions (the `potion_reserve` directive, default 2). Below that, the planner adds a `buy` op before any trip away from town, and `Shop` carries it out (A61). A potion is drunk out of combat only when no food is near and the next goal needs the health.
- **Safe zones.** Health returning in a safe zone has not been observed yet. `Heal` (A10) measures it: a "yes" is saved to the knowledge base, a "no" (200 ticks in a safe zone with no health back) holds for that run. If it returns, healing while exploring safe ground is the free fallback; if not, the fallback is potions and food, and buying more is the planner's `buy` op (A61): Heal raises nothing on its own.

**Raising health and protection over time:**
- **Armor first.** Defense counts twice: it lowers the chance to be hit and the damage of each hit. `Equip` scores armor by the damage it would have saved against the threats in the item and threat tables, and the gem budget puts armor and potions ahead of curiosity spending.
- **Max health rises on a level's first clear** (`level_clear_ceremony.max_health_gain`), so clearing levels is also how the agent grows. Whether any supply raises max health permanently is an open question in GAME_NOTES; the plan counts on none.
- **Extra lives** are hearts, consumed on pickup into the lives counter (M §11). In Olympuff they drop from cut grass and bushes (M §16). A heart in sight and safe to reach is a top `Loot` target once its ground code is observed (A47 learns it in play, A57 confirms it); until then Loot scores gems first.

**After a death.** `Recover` runs only when the chest's spot is safe enough at full health: no group of hostiles still there, and not deep in fog. Otherwise the agent re-equips from town and gets the chest later, or writes it off. Every death is logged with its cause and the decision that led to it, and it tightens the risk level for that hostile type.

### Combat

- **Our side.** Reach is `attack_range` from `get_self`, refreshed after `Arm`; a `target_out_of_range` result also reports reach and distance. Our hit chance is published: d20 + attack ≥ 10 + target defense, 65% against a hostile at the world's base attack power of 2. The win estimate's damage a swing is that chance times the mean of 1 up to attack power plus the armed weapon's published damage (A81); the largest hit per NPC type is learned from `NPCDamaged` for Equip.
- **Their side.** Damage per hit, interval and reach per NPC type are learned from `Damaged` and `Attacked` events and kept in the knowledge base. Hostile stats are never served. A hostile's damage number is its attack power, so the win estimate prices its swing on the same roll as ours, with its largest measured hit as that number and as the size of each landed hit (A81).
- **Win estimate.** Ticks for us to kill everything that will join, versus ticks for that group to kill us. Fight above a margin, flee below it. Directives can bias the margin. No NPC's health is served except a boss's (M §9.3), so a type's health is learned as the total `NPCDamaged` it took before `NPCDied`; until a type has a kill on record, its health is assumed to be the largest measured for any type, and before any kill at all, 10 (a new character's health; an assumption, not a measurement). Either way the type falls under "conservative until measured".
- **Conservative until measured.** At 10 health with no armor, the default is to fight only a lone hostile of a type already measured, or a new type from full health with an escape queued.
- **Where to hunt.** Hunting grounds advertise a strength ceiling (`get_zone`); pick the highest ceiling at or above our strength that the win estimates say we can win in. Our strength is served only on the owner watch sheet (M §5.5), outside the character's routes, so the agent does not read it: that would be a request outside the character call budget (PLAN.md **Server gaps**). Strength is bracketed by probing instead: an `over_strength_ceiling` rejection means our strength is above that ceiling, a successful entry that it is at or below. The bracket is reset after an `Equip` change. Fields near town are the next step up.
- **Never fight from a safe zone** (rejected: `not_allowed_in_safe_zone`, M §11; GAME_NOTES Zones). Use one to recover, and as a step-away escape when it is adjacent.
- **Bosses.** A boss shows `health`/`max_health`, so progress is measurable. A fight is on a clock, one at a time, and the door may be contested. The `Boss` state needs explicit preconditions from the plan: health, potions, gear, and the means to reach the boss.

### Gear and items

- **Item table.** Keyed by `supply_subtype_code`, filled by observation: reach and damage after `Arm`, damage taken after `Wear`, shop prices seen, and which capability the item has (cut, chop, smash, burn, blast, light, water). A18 stores reach, price, the largest weapon hit per NPC type, and for armor worn alone `damage_taken`, `damage_without` (nothing worn, after that item came off) and `damage_saved` (their difference) per NPC type; A46 stores the capabilities a subtype has opened a block with; capability tags from the Manual's Supplies reference (saims B132) are A54.
- **Equip** scores each slot and swaps when a carried item beats the worn one. Consumables (potions, food) are kept for `Heal`.
- **Budget.** Gems are kept through death and gear is not, so the plan spends gems on what most raises survival first (weapon, armor, potions), then on tools a clue asks for.
- **Compose.** When any fragment is held, its `fragment` field names the whole and the missing slots. The plan tracks it as a goal, and `Solve` composes when the set is complete.

### Knowledge base

A JSON file per world, `python/.state/worlds/<world_code>.json`, gitignored, shared by every character of that world run from this checkout. It sits apart from the per-run trace files (`python/.state/<profile>.<character_id>.trace.jsonl`, A59):

- Revealed terrain per map, entrance marks, doors and where they lead, safe tiles, hunting grounds and ceilings, shops and prices.
- Clues: the text of every sign, statue, scroll and helper line, with where it was found and when. `clues` is a list of `{kind, text, map_id, x, y, tick}` rows, plus `speaker_id` on a helper line or `supply_id` on a scroll; a sign is stored once per cell, a scroll once per supply, a helper line once per speaker and text (PLAN.md A32, A56). Each new row also queues `{"trigger": "clue", …row}` on the character's `Memory.clue_signals`, which the strategist drains (A35).
- `read_cells`: `{"<map_id>": ["x,y", …]}`, readable cells whose `Read` applied, `spoken_npcs`: NPC ids a `say` op's `Say` applied to, so `Investigate` never repeats one (PLAN.md A30), and `greeted_npcs`: NPC ids Greet's hello applied to, kept apart so a hello never settles a `say` op (A65). Each grows by one entry per sign or NPC in the world.
- Scroll discovery (A56): `seen_supply_codes`, `probed_supply_codes`, `scroll_subtype_codes`, and `read_supplies` so subtype codes are learned once and scroll text is not re-read.
- Break attempts per (block, capability), and the result.
- `gem_yield` (A63): `{"version": 2, "<map_id>": {"cuts": [{x, y, block, tick, gem}, …], "regions": {"<rx>,<ry>": {cuts, gems, last_tick}}}}`, every block the agent cut and whether a gem came of it (the latest 500 per map), and grass cuts summed per 16×16-block region; a region with no gem after 15 cuts (ground at the manual's 20% would almost surely have shown one) is barren, and Gather skips it (A81). A store of another `version` (saved before A81, when bush cuts counted at the old rates) reads as empty and is replaced on the next cut.
- NPC type stats, item stats, compose results, and what each entrance turned out to need. `npc_types` holds `{"<npc type code>": {"hostile": true}}` for every type that has swung at, hit us or died in view, and `hostile_sightings` `{"<npc id>": {code, map_id, x, y, tick, home, post, reach}}` for the NPCs of those types: where each was last seen, its post and the reach it hit us from. Both are loaded into the world model at start and written back at exit (`hostile_memory`; free-play run 6).
- `items`: one row per `supply_subtype_code` with `attack_range` (from a `target_out_of_range` rejection, under the weapon armed in that response) and `gem_price` (from supplies seen), each overwritten by the latest value; `heal_amount` (largest health gain seen from a `Take` or self-`Use`) and `heal_on_pickup` (whether pickup healed while hurt, A24); `weapon_damage`, the max observed hit per `npc_type_code` from an `NPCDamaged` matched to our `Use`; and for armor, `damage_taken` (worn alone), `damage_without` (nothing worn, filed under the item that just came off) and `damage_saved` (their difference) per `npc_type_code` from `Damaged`, skipping any response whose worn loadout changed (PLAN.md A18).
- Level progress: which levels are cleared, and the route and solution for each.

This is what makes a second run better than the first, and it is what the strategist reads. None of it is committed.

### Strategist (the AI planner)

- **Runs** in its own thread, on in every live run (`--no-planner` is a test mode). It owns the goal stack. The state machine keeps the old plan until a new one lands; with no valid plan the stack is empty and the dispatcher's safe default runs (exploring in safe ground).
- **Triggers:** a new clue, an NPC reply, a new map or level, getting hurt, no progress for N minutes, a goal finished or dropped, a death, and a 15 s timer. Calls are budgeted per minute of play, in calls and tokens (PLAN.md A35): tokens times the model's price is the cost per minute, whatever the model costs, and a long session never runs dry.
- **Input:** a compact state summary, relevant knowledge-base entries, all clue text, the current plan, and the operator's directives.
- **Its main job** is interpretation: turn clue text (riddles and directions) into concrete goals, such as which entrance mark matches a clue, what tool an entrance needs, or which odd block to try.
- **Output:** JSON checked against a schema. The example uses an invented world:

  ```json
  {"goals": [{"op": "say", "npc_type": "guard", "text": "hello"},
             {"op": "buy", "code": "torch", "why": "clue: the cave is dark"},
             {"op": "travel", "to": "entrance", "x": 120, "y": 40},
             {"op": "break_block", "x": 118, "y": 41, "capability": "burn"}],
   "params": {"fight_margin": 1.5, "retreat_hits": 2, "curiosity": 0.2,
              "lives_floor": 3, "risk": 0.5, "potion_reserve": 2},
   "notes": "The sign says the door is behind the burnt hedge."}
  ```

  Top level has exactly three keys. `goals` replaces the goal stack, tried in order. `params` is applied at once, within the limits below. `notes` is free text for the trace. Everything the agent should do, speech included, is a goal op; a `set_param` op changes a param only when the goal stack reaches it, within the same limits.

- **Param limits.** The strategist may only tighten survival params, never loosen them past the directives file's value (or the default when the file sets none): it may raise `fight_margin`, `retreat_hits`, `lives_floor` and `potion_reserve`, and lower `risk`. `curiosity` it may set anywhere in its range. A value that loosens a survival param, or is out of range (see the param table), is dropped and logged, and so is a `set_param` op that would loosen one. The next prompt's State repeats each such rejection with its reason and the param's meaning (`last_reply_rejected`), so the planner learns which way a param goes. Only the directives file loosens them.

- **Operations** form a fixed set. Each maps onto a state and has fixed fields; every op may also carry `why` (free text, logged). An op with an unknown name, a missing field, or a field of the wrong type is dropped and logged.

  | Op | Fields | State |
  |---|---|---|
  | `travel` | `to` (`entrance`, `town`, `hunting_ground`, `shop`, `point`), `x`, `y` (needed only for a `point`; `town` and `hunting_ground` take none; `shop` and `entrance` without them mean the nearest known), optional `map_id` | `Travel` |
  | `explore_area` | `x`, `y`, `radius` | `Explore` |
  | `read` | `x`, `y` for a readable cell, or `supply_id` for a scroll | `Investigate` |
  | `say` | `npc_id` or `npc_type`, `text` | `Investigate` |
  | `buy` | `code` (a `supply_subtype_code`) | `Shop` |
  | `break_block` | `x`, `y`, `capability` (`cut`, `chop`, `smash`, `burn`, `blast`) | `Break` |
  | `use_block` | `x`, `y`, `code` (the supply to arm, such as a key) | `Solve` |
  | `compose` | `composes_into` | `Solve` |
  | `fetch_item` | `code`, optional `x`, `y` | `Loot` |
  | `gather_gems` | `count` | `Gather` |
  | `hunt` | `npc_type`, optional `x`, `y` of the ground | `Fight` |
  | `enter_level` | `x`, `y` of the entrance | `Level` |
  | `fight_boss` | `x`, `y` of the boss door | `Boss` |
  | `avoid` | one of `npc_type`, `block_type`, or `x`, `y`, `radius` | Cost grid |
  | `wait` | `seconds` (at most 30), `why` (required) | `Idle` |
  | `set_param` | `name`, `value` | none |
- **Packaging:** the provider SDK (`anthropic`) is the planner's one dependency, imported only when the planner runs, so the core and `make test` stay standard library only. With `--no-planner` (a test mode), the plan comes from the character file and simple built-in rules.

### Runtime directives

A per-character file, `characters/<name>.directives.toml`, re-read whenever it changes:

```toml
params = { fight_margin = 2.0, retreat_hits = 2, curiosity = 0.2, lives_floor = 3, risk = 0.5, potion_reserve = 2 }
never_attack = ["character"]                            # hard constraint, enforced by state guards
goals = ["gather_gems:20", "buy:bronze_mail"]           # replaces the goal stack
instructions = """
If you find a sign with numbers on it, try them as a code at the nearest locked door.
"""                                                     # free text, passed to the strategist
```

Structured keys take effect on the next round trip with no model involved. Free text only steers the strategist. `never_attack`, a `gather_gems` goal (**Gather**, A22), and the survival `params` **Retreat** reads (A9) and `fight_margin` (**Fight**, A23) change behavior today. `goals` replace the goal stack when present (A34); shorthand like `gather_gems:20` and `buy:torch` is parsed into typed ops. A reload with unchanged `goals` keeps the stack's progress. With no valid `goals`, the built-in plan mirrors the character's `policy.goals`. Until the states behind them ship, only `explore_area`, `travel` (`entrance`, `town`, `point`) and `wait` run from the stack; other ops are dropped and logged when they reach the top, and so is an op with no path for 30 seconds. **Gather** reads `gather_gems` from `goals` directly, so dropping it from the stack does not stop it. `wait` seconds convert at the world's `tick_rate_hz`. `instructions` are kept for the strategist (A35).

Hard constraints are never free text. `never_attack` lists what may not be attacked: `character`, or NPC type codes. `Fight` and `Boss` guards refuse such a target, and the executor drops any `Use` aimed at one, whatever the strategist says, and with the strategist off. The strategist cannot change it: a `set_param` naming it is dropped.

| Param | Default | Range | Strategist may | Meaning |
|---|---|---|---|---|
| `fight_margin` | 1.5 | ≥ 1 | raise | Our ticks-to-win must beat theirs by this factor |
| `retreat_hits` | 2 | ≥ 1, integer | raise | Retreat when this many expected hits could kill |
| `curiosity` | 0.2 | 0 to 1 | set | Share of ticks `Investigate` and `Break` may use (see Curiosity) |
| `lives_floor` | 3 | ≥ 1, integer | raise | At or below this many lives, fight only measured weak hostiles and stay inside explored ground |
| `risk` | 0.5 | 0 to 1 | lower | 0 cautious to 1 bold; scales the fight margin, retreat threshold, untested types and level entry (see Health and lives) |
| `potion_reserve` | 2 | ≥ 0, integer | raise | Potions to keep. Planner-only since A61: the planner adds a `buy` below it; no state restocks on its own |

A directives value out of range is ignored and logged, and the default stays.

## Milestones

These are the milestone groups. PLAN.md **Milestones** splits each into PR-sized items with their own IDs (A1, A2, …) and dependencies, and ends each with an acceptance item that runs the done-when below. This table owns the scope and the done-when.

| ID | Milestone | Done when |
|---|---|---|
| M0 | **Discovery.** Docs read, hand play through MCP, [GAME_NOTES.md](GAME_NOTES.md) written. | Done: every unknown above has an answer or a measurement to take |
| M6 | **Executor.** `Step`/`Wait` pacing, multi-intent queues, two poll cadences, deltas and complete snapshots, health tracking. A live smoke test against Olympuff. | A character walks 200 blocks with no `movement_cooldown` rejections, sending `POST tick` in under a quarter of its calm windows (reads on spare tokens excluded) |
| M7 | **State machine and survival.** Replace `brain.decide` with prioritised states: `Sync`, `Downed`, `Escape`, `Retreat`, `Heal`, `Flee`, `Recover`, `Explore`. Threat table (damage per hit per hostile type, from `Damaged`). Safe-tile discovery: `get_zone` on cells around the respawn point and along the route, within the call budget, so `Retreat`, `Flee` and `Heal` have known safe tiles (the wider interest-list `get_zone` stays in M10). Cost-grid planner with break costs inert, rejection learning, stuck detection and escalation steps 1, 3 and 5. Trace replay tests and the navigation fixtures. | Survives an hour in the overworld, retreating to a known safe tile before the next `retreat_hits` hits could kill by its threat table, and recovering its chest only when the spot is safe; it measures whether health returns in safe zones and heals by `Heal`'s no-purchase fallbacks (a new character carries only its knife, so potions wait for `Shop` in M8); reaches a point 150 blocks away through fog and walkable detours, or gives up with a reason, never loops. A route that needs a block broken counts as a give-up with that reason |
| M8 | **Gear, economy and combat.** `Gather`, `Shop`, `Loot`, `Equip`, `Fight` with group-aware win estimates and the retreat queued; learned item table; healing from food. | Earns gems, buys armor, a weapon and a potion reserve, heals from food it picks up and from carried potions, and kills lone weak hostiles without dying; never starts a fight below its health floor |
| M9 | **Navigation and knowledge.** Per-world knowledge base, overworld `Travel` to entrance marks and back to town, door graph and cross-map routing, break memory per (block, capability), `Break` (escalation steps 2 and 4; break costs go live in the grid). | Visits every entrance mark within its strength, records what each needs, and returns to town |
| M10 | **Curiosity and clues.** Interest list, odd-block detector, `Investigate` and `Break` under the curiosity budget, clue capture with place and time, no-LLM clue rules. | Every readable cell that came into sight along its route has been read, and every NPC that came within 25 blocks spoken to; it finds and opens an odd block in a test map, and never tries the same capability twice on the same block (a capability that failed there; one that opened it may cut the regrown block again). Gate: `make smoke-m10-olympuff` and `m10_acceptance.py` (PLAN.md A33, README) |
| M4 | **Strategist and directives.** LLM planner thread, plan schema, directives file, trace logging. | Given clues from a test world, it plans the right `buy`/`travel`/`break_block` operations and the state machine carries them out |
| M11 | **Levels.** `Level`, `Boss`, `Solve` (`Compose`, keys at doors). Boss preconditions from the plan; boss progress from its `health`. | Clears the easiest open level unattended, then uses what it learned to attempt the next |
| M12 | **Evaluation.** Metrics per run (levels cleared, deaths, kills, gems, time per level), compared across commits. | A regression shows up as a number |

M6 and M7 come first whatever else changes. M8 to M11 now have known shapes. What remains uncertain is the order of levels in each world, and the strategist learns that.

## Risks

- **The early game is lethal.** Ten health against hostiles that gang up means one bad engagement costs a life. Lives are finite on live worlds (the observer character is down to 5). Mitigation: conservative defaults, an escape queued with every attack, and practice on the sandbox before Olympuff.
- **Latency kills.** Hand play through MCP lost a life to a 4 s round trip. The executor must poll every tick while threatened; this is an M6 requirement, not a tuning detail.
- **Clue interpretation is the hard part.** Riddles and directions are written for people. Without the strategist the agent can still gear up, hunt and walk to entrances, but it will not know what a locked or hidden entrance wants. Keep the no-LLM path useful; accept that levels need the strategist.
- **Spoilers.** Real-world clue text must never reach the repo, the tests or the PR text. Fixtures use invented worlds; the knowledge base stays in gitignored `.state/`.
- **Getting stuck.** Fog, regrowing blocks and NPCs in corridors will trap a naive walker. The Navigation section makes "stuck" a detected state with fixed escalation and a give-up, never a silent loop.
- **LLM cost and latency.** Strict triggers and a budget cap; the agent must play acceptably with the strategist off.
- **Lives are finite.** The risk level follows the lives left, and a floor stops fights and deep exploration near the end. Losing a life is logged as a decision to review, not just a counter going down.
- **Learned stats are noisy early.** Keep conservative defaults (flee more, fight less) until the tables have samples.
