# Plan: an agent that can actually play

Goal: a reference agent that survives, gears up, fights, travels the overworld, solves clues, and clears levels. "Clear all `level_count` levels" is how a world is beaten (`GET /world`); Olympuff has 8.

The game facts this plan relies on are in [GAME_NOTES.md](GAME_NOTES.md), with sources.

## Verdict on the approach

A deterministic state machine for play, plus an LLM that plans off the tick, is a good fit for this game. It is the standard hybrid of fast reactive control and slow deliberation, and the API is shaped for it. Intent queues carry up to 4 s of actions, rate limits allow one request per tick, and the game runs at 10 Hz. So no model call can ever sit on the tick path, and none needs to.

Five rules keep it working:

1. **Use priority states, not a flat FSM.** Survival interrupts first, opportunities second, the plan's goal last. A flat graph of every state-to-state transition grows too large to maintain.
2. **The LLM never emits intents.** It emits typed plan operations from a fixed vocabulary (go here, read that, buy this, break that block with this tool). The state machine turns them into moves. Unknown or invalid operations are dropped.
3. **Replays stay deterministic.** Every LLM answer and directive change is written to the trace, so a run can be replayed without the model.
4. **The rules are known; the world must be learned.** Milestone 0 answered how levels, combat, items, `Compose` and death work. What each world hides (which entrance needs what, where the secrets are) is told only through in-world text: signs, statues, scrolls, helper lines. Reading that text and acting on it is the strategist's job, and the knowledge base is where it accumulates.
5. **No spoilers in the repo.** The repo is public. World clues, puzzle answers and level details live only in the gitignored per-world knowledge base. Tests use invented worlds.

## What milestone 0 settled

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
| `Step` + `Wait` pacing (base 2.5 blocks/s means a step every 4 ticks) | No: sends a move every window, so many are rejected `movement_cooldown` | Moving at all |
| Multi-intent queues | No: always one intent | Freeing the request budget; queuing a retreat with an attack |
| Snapshot deltas (`snapshot_version`), health in the observation | No: separate entity reads, health never tracked | Perception, retreat |
| `Arm`, `Wear`, `Remove`, `Drop`, `attack_range` | No | Gear |
| `Use` on a block (attacks the NPC on it, or breaks the block) | No: flees from every NPC | Fighting, opening the way |
| Priced supplies (`gem_price`) | No | Buying gear, potions, tools |
| `Read` (signs, statues, scrolls), `Say` to an NPC | No | Clues |
| `Compose`, fragment slots | No | Composite keys |
| `locked` doors, traps and `Disarm`, `occupy_damage` | No | Level obstacles |
| Zones: `safe`, hunting-ground strength ceiling | No | Where to rest, where to hunt |
| Minimap: level entrance marks | No | Overworld travel |
| `Sleep`, respawn delay, lives | Partly | Ending a session, death handling |

## Architecture

```
            ┌────────────── strategist (LLM, optional, async) ──────────────┐
 directives │ triggers: clue read, NPC spoke, new level, stuck, goal done, │
 (hot file) ┤ death. In: state summary + knowledge. Out: plan operations. │
            └──────────────────────────────┬────────────────────────────────┘
                                           ▼ validated plan (goal stack + parameters)
  knowledge base ◄──── world model ◄──── perception (tick deltas, rare terrain reads)
  (per world, .state/, gitignored)
                            │
                            ▼
                    state machine  ── every tick: pick state by priority, state.act()
                            │
                            ▼
                    executor ── builds paced intent queues, sends one round trip,
                                re-sends only when the queue is invalidated
```

### Executor (fixes milestone 1)

- **Paced queues.** Paths become `Step`, `Wait`×n, `Step`, … queues. The number of waits comes from `movement_speed`. Attacks are paced by the weapon cooldown (10 ticks by default), and block breaking spends the same attack accumulator. Speech is paced at 10 ticks. The queue is cut at the world's horizon.
- **Re-send only when the queue goes wrong.** That means a result is rejected, a delta changes something on the path, or the state changes. A rejection discards the rest of the queue, so a path is rebuilt from the current position, not resent.
- **Two cadences.** While calm, poll every 4–10 ticks and spend spare tokens on terrain reads. While a hostile is within 3 blocks or health is dropping, poll every tick.
- **Snapshots.** Deltas come only against the previous version, so the slow cadence gets complete snapshots. The world model applies both shapes.
- **Attack queues carry their own escape.** An attack queue ends with the retreat steps, or is no longer than the next poll. A slow poll must never leave the character swinging after the fight turned.
- **Terrain reads.** With perception 25 the terrain window is 51×51, so terrain is read only on a map change or after moving about half the window.
- **Deltas feed the model.** Health, inventory and entity changes are folded into the world model from them.

### State machine

States are checked in priority order each tick. The first whose guard holds runs; each has entry and exit conditions with hysteresis so it does not flip back and forth.

| Priority | State | Enters when | Does |
|---|---|---|---|
| 0 | `Sync` | Unplaced, position unknown, after a warp | Reads self and position, waits for placement |
| 0 | `Downed` | `Died` | Waits for `Respawned`, then queues `Recover` |
| 1 | `Escape` | Standing on damage, or trapped | Steps off; crosses as little hazard as possible |
| 1 | `Retreat` | The next two expected hits could kill, or the threat outclasses us | Goes to the nearest known safe tile, then `Heal` (see Health and lives) |
| 1 | `Heal` | Hurt and no hostile in range, or health too low for the next goal | Eats nearby food, else drinks a potion from the reserve (`Arm` + `Use` self), else rests in a safe zone |
| 2 | `Fight` | A hostile is in range and the win estimate clears the margin, counting every hostile within 2 blocks of it | Closes to `attack_range`, `Use` on the NPC's block, with the retreat queued behind |
| 2 | `Flee` | A hostile is in range and we would lose | Opens distance toward safety; safe zones stop all damage |
| 3 | `Recover` | Our death chest is on a reachable map | Walks next to it (a safe tile next to it is enough), `WithdrawFromChest` |
| 3 | `Equip` | Carrying something better than what is worn or armed | `Arm`, `Wear`, `Remove`; armor scored by damage it would have saved |
| 3 | `Loot` | A worthwhile free supply or chest is near enough | Walks, `Take` or `WithdrawFromChest`, `Drop` junk when full |
| 3 | `Shop` | The plan wants an item that is in sight with a `gem_price` we can pay | Walks onto it or `Take`s it |
| 4 | `Investigate` | The interest list has an item within the curiosity budget (see Curiosity) | `Read`, `Say`, `get_zone`, walk to look; stores text as a clue |
| 4 | `Break` | The plan names a block to open, or the odd-block detector scores a nearby block high enough | Arms the matching capability, `Use` on the block, records the result per block |
| 4 | `Solve` | The plan holds an action to try (compose, a key at a door, a tool at a block) | Carries out the plan operation and checks the result |
| 5 | `Gather` | The plan needs gems | Cuts grass and bushes and visits gem piles in safe-ish ground |
| 5 | `Level` | Inside a level | Walks the rooms toward the unexplored doors, using `Fight`/`Break`/`Investigate` as they apply |
| 5 | `Boss` | At a boss door with the plan's preconditions met | Enters, fights within the clock; retreats out only if possible, else commits |
| 5 | `Travel` | The plan names a destination (entrance, town, hunting ground, shop) | Cost-grid planner through fog, door graph across maps, stuck detection and escalation (see Navigation) |
| 5 | `Explore` | Nothing else | Frontier exploration, unvisited entrance marks first; frontiers that turn out unreachable are dropped with a backoff |
| 6 | `Idle` / `Sleep` | Stopping, or the plan says wait | Sends nothing, or `Sleep` at shutdown (not allowed inside a level) |

Every state is a small class with `guard(world, plan) -> bool`, `act(world, plan) -> Queue | None` and `done(world) -> bool`. They are tested the way `brain.py` is tested today: a model in, an intent queue out.

### Navigation and getting unstuck

Paths are never straight lines. Bushes, trees, water, walls, fences, NPCs and other characters are in the way, and most of the map starts as fog. Today's `world.path` is A* over tiles already seen. It returns nothing when the goal is in fog or walled off, and it keeps off a rejected tile for only one decision. So the agent can go round obstacles it has seen, but it can't head for somewhere it hasn't seen, and nothing notices that it has stopped making progress. The fix comes in four parts.

**1. One planner over a cost grid, not a walkable/blocked map.**

| Cell | Cost |
|---|---|
| Known walkable | 1 |
| Fog (never seen) | 2. Assumed open, so the agent can aim at an entrance 300 blocks into the fog |
| A breakable obstacle we hold the capability for, not marked as failed in break memory | Break time plus 1, so a bush in a hedge line is a door, not a wall |
| `fire`, `lava` | 1 plus a cost per point of `occupy_damage`; entered only when there is no other way |
| Near a hostile | A danger cost that falls off with distance, so routes keep away from hostiles |
| NPC or character standing there | High but finite, and it expires: they move |
| Known blocked, or marked unreachable | Impassable |

- **Walk only the part of the path we have seen.** The executor walks the known prefix and replans when terrain reads reveal what lies ahead, or a step is rejected. Fog optimism is corrected by looking.
- **Long trips are two-level.** A coarse search over 16×16 blocks (the server's own tile size) picks the corridor; A* inside the perception window picks the steps. Each search has a node budget per tick, so a long route never stalls a tick.

**2. Rejections teach the map, by code.**

| Rejection | What the agent records |
|---|---|
| `not_traversable` | That cell is blocked until a `BlockChanged` says otherwise |
| `block_occupied` | Cost on that cell for a few seconds; wait one move, then route round |
| `conflict_lost` | Retry next move |
| `door_locked` | Door needs a key; recorded in the knowledge base, not retried |
| `over_strength_ceiling` | Zone closed to us at this strength |
| `would_strand` | Step to land first |

An opening we cut or burned is open only until it grows back (about 60 s for a bush). It is planned through with that deadline, never as permanent.

**3. Stuck detection.** Progress is the remaining path cost to the goal, measured each move. The agent is stuck when any of these hold:
- the remaining cost hasn't fallen in 20 moves or 30 s;
- the same few cells keep being revisited (oscillation);
- 3 moves in a row are rejected;
- the planner finds no path, even with fog assumed open.

**4. Escalation, in order, each step only if the one before fails:**
1. Replan with the learned blocks, and with fog optimism lowered so known ground is preferred.
2. Break through: if a breakable obstacle lies on the best blocked route and we hold the capability, `Break` it. A failed try is recorded per block and never repeated.
3. Reveal: explore the frontier cells nearest the goal, following the wall of the obstacle (left-hand rule) for a bounded number of moves, to uncover a way round.
4. Change the means: if the goal is enclosed on this map (water, cliffs, a locked door), record what seems to be needed (a raft, a key, a door from another map) and route through the door graph if one is known.
5. Give up for now: mark the goal unreachable with an exponential backoff, pick the next goal, and raise the strategist's `stuck` trigger. The trigger carries the goal, the explored outline and the blocking cell types. The strategist may answer with a tool to buy or a different route.

**Cross-map routing.** Doors are edges of a graph. Each warp records where it landed, unvisited doors are exploration targets, and a route is a search over the door graph, then A* on each map.

**Escape.** If regrowth or a crowd closes the agent in, `Escape` breaks out with whatever capability it holds, or waits for the blocker to move. Waiting inside a safe zone costs nothing.

**Tests.** Fixtures with:
- a U-shaped trap: a local minimum that greedy moves fall into;
- a maze;
- a goal behind a hedge line, with and without the tool;
- a goal enclosed by water;
- an NPC parked in a one-wide corridor;
- a corridor that is a dead end once fog is revealed.

Each asserts the goal is reached, or abandoned with the right reason, within a move budget.

### Curiosity

Progress in this game is hidden behind things a player has to poke at. Helpers drop hints only when spoken to. Signs and statues carry text. Breakable "odd blocks" hide gems, doors and secrets. Nothing announces itself (M §16). So the agent needs a drive to investigate, not just a reflex for whatever it passes.

**Interest list.** Every terrain and entity read adds to it, and the knowledge base remembers what has been done, so nothing is investigated twice.

| Thing | Action | Notes |
|---|---|---|
| Unread `readable` cell (sign, statue, plinth) | `Read` | From up to 25 blocks away, no walk needed. Free: no speech cost, one per tick |
| Unread scroll, carried or in sight | `Read` | |
| NPC id never spoken to | `Say` once | Works from 25 blocks. A helper replies with its line; a hostile stays silent, which also tells us it is not a helper. Spaced 1 s apart |
| Supply type never seen | Walk over or `Take`, if free and safe | Fills the item table |
| Odd block out | Try each capability we hold, cheapest first | Detector below |
| Cell with unusual art, or a statue facing differently from its neighbours | Investigate the cells around it | Art is a public picture and authors use it as a clue (M §9.2) |
| Unvisited door or entrance mark | Walk to it, look | A locked or hidden door gets recorded with what it seems to need |
| Unknown zone | `get_zone` once | Finds safe zones and hunting grounds |

**Odd-block detector.** This is the manual's motif: one rock in a garden, one bush in a wheat field, one tree in a maze (M §16). A block is odd when:
- its type is rare in its 7×7 neighbourhood (one or two of it);
- most of the surrounding cells are a single different type or art;
- it is breakable in principle: bush, tree, rock, mountain or wall, not water or a door.

A boost comes from a clue that mentions its type or surroundings ("rings hollow", "behind it"). The detector over-reports on purpose. The knowledge base records each try, so a false positive costs a few ticks once.

**Trying a block.**
- Weapons first, since they are free and not used up: a sword cuts and chops, a mallet smashes.
- Then tools, which are used up: matches (5 gems), then bombs (expensive). A tool is spent only if the block scores high or a clue points at it.
- Every outcome is stored per block: `applied_no_effect` with which capability, or what it turned into and what dropped.
- A capability we lack, on a high-scoring block, becomes a `buy` suggestion for the strategist.

**Budget, so curiosity doesn't get it killed or stalled.**
- Each item scores value × novelty ÷ (distance + danger + consumable cost).
- `Investigate` and `Break` run only when survival and the current goal allow, and get a capped share of time. A directive param `curiosity`, default 0.2, sets that share.
- Reading and speaking from where the agent stands are nearly free, so they always happen. Detours are what the budget limits.
- Above the cap, items wait for idle moments, or for the strategist to promote one.

**Where the results go.** Every line read or heard goes to the clue list with its place, and fires the strategist's `clue` trigger. The strategist turns clues into goals. With no LLM, simple rules still apply: a clue naming a direction biases `Explore` that way, and a clue naming a capability raises that capability's priority on nearby odd blocks.

### Health and lives

Health is the resource every other decision spends, and lives are the budget behind it. On a live world, at zero lives the character is gone for good. The agent tracks both and acts to keep them up.

**What it tracks.** `health` and `max_health` arrive in every round trip's observation while awake (the current agent ignores them). `lives` is in the snapshot too. Each `Damaged` event is logged with its source, so the agent knows what is hurting it and how fast.

**Lives set how bold it is.** A single risk level, from cautious to bold, follows the lives left. It scales:
- the fight margin;
- the retreat threshold;
- whether it engages a hostile type it hasn't measured;
- whether it enters a level at all.

Below a floor, 3 lives by default, it stops fighting anything but measured weak hostiles and goes no further than the edge of explored ground. The `lives_floor` and `risk` directives set these.

**Protecting health in the moment:**
- **Never start a fight hurt.** `Fight` needs health above the expected damage of the whole group over the fight, plus a margin. Otherwise heal first.
- **Retreat in time.** The threshold is set in hits, not percent: when the next two hits from what is attacking could kill, it leaves. The steps away are already queued behind every attack (see Executor).
- **Drink mid-fight** only when retreating is impossible, such as a boss room or being cornered. Swapping in a potion costs a tick, drinking costs another, and re-arming the weapon a third, so the agent compares those three ticks of incoming damage with the heal.
- **Step off damaging ground** at once (`Escape`). Fire and lava are crossed only when the route has no other way.
- **Traps.** Wear goggles when they are owned and the area is trapped. Never walk on a seen armed trap.
- **Never stand around exposed.** `Sleep` in a safe zone when the run ends, since an awake, unattended character keeps taking hits. Idle waits happen in safe zones.

**Healing:**
- **Food first, it's free.** Food lying in sight is eaten when the amount missing is at least what it heals, and is remembered as a source. How much each type heals is learned from the `health` change.
- **Potions are a reserve.** The agent keeps N potions (the `potion_reserve` directive, default 2). Below that, `Shop` buys more before any trip away from town. A potion is drunk out of combat only when no food is near and the next goal needs the health.
- **Safe zones.** Health returning in a safe zone has not been observed yet. Milestone 2 measures it. If it returns, resting in town is the free fallback.

**Raising health and protection over time:**
- **Armor first.** Defense counts twice: it lowers the chance to be hit and the damage of each hit. `Equip` scores armor by the damage it would have saved against the threats in the item and threat tables, and the gem budget puts armor and potions ahead of curiosity spending.
- **Max health rises on a level's first clear** (`level_clear_ceremony.max_health_gain`), so clearing levels is also how the agent grows. Any other permanent gain is a supply consumed for it, and the agent uses such a supply as soon as it finds one.
- **Extra lives** are auto-consumed supplies. Any seen within reach becomes a top-priority `Loot` goal.

**After a death.** `Recover` runs only when the chest's spot is safe enough at full health: no group of hostiles still there, and not deep in fog. Otherwise the agent re-equips from town and gets the chest later, or writes it off. Every death is logged with its cause and the decision that led to it, and it tightens the risk level for that hostile type.

### Combat

- **Our side.** Reach is `attack_range` from `get_self`, refreshed after `Arm`; a `target_out_of_range` result also reports reach and distance. Our hit chance is published: d20 + attack ≥ 10 + target defense. Our damage per hit is learned from `NPCDamaged`.
- **Their side.** Damage per hit, interval and reach per NPC type are learned from `Damaged` and `Attacked` events and kept in the knowledge base. Hostile stats are never served.
- **Win estimate.** Ticks for us to kill everything that will join, versus ticks for that group to kill us. Fight above a margin, flee below it. Directives can bias the margin.
- **Conservative until measured.** At 10 health with no armor, the default is to fight only a lone hostile of a type already measured, or a new type from full health with an escape queued.
- **Where to hunt.** Hunting grounds advertise a strength ceiling; pick the strongest one we can win in. Fields near town are the next step up.
- **Never fight from a safe zone** (rejected). Use one to recover, and as a step-away escape when it is adjacent.
- **Bosses.** A boss shows `health`/`max_health`, so progress is measurable. A fight is on a clock, one at a time, and the door may be contested. The `Boss` state needs explicit preconditions from the plan: health, potions, gear, and the means to reach the boss.

### Gear and items

- **Item table.** Keyed by `supply_subtype_code`, filled by observation: reach and damage after `Arm`, damage taken after `Wear`, shop prices seen, and which capability the item has (cut, chop, smash, burn, blast, light, water).
- **Equip** scores each slot and swaps when a carried item beats the worn one. Consumables (potions, food) are kept for `Heal`.
- **Budget.** Gems are kept through death and gear is not, so the plan spends gems on what most raises survival first (weapon, armor, potions), then on tools a clue asks for.
- **Compose.** When any fragment is held, its `fragment` field names the whole and the missing slots. The plan tracks it as a goal, and `Solve` composes when the set is complete.

### Knowledge base

A JSON file per world in `python/.state/`, gitignored, shared by every character of that world:

- Revealed terrain per map, entrance marks, doors and where they lead, safe tiles, hunting grounds and ceilings, shops and prices.
- Clues: the text of every sign, statue, scroll and helper line, with where it was found and when.
- Per-block break attempts: which capability was tried on which block, and the result.
- NPC type stats, item stats, compose results, and what each entrance turned out to need.
- Level progress: which levels are cleared, and the route and solution for each.

This is what makes a second run better than the first, and it is what the strategist reads. None of it is committed.

### Strategist (LLM, optional)

- **Runs** in its own thread. The state machine keeps the old plan until a new one lands.
- **Triggers:** a new clue, an NPC reply, entering a level, no progress for N minutes, a goal finished or failed, a death. Calls are rate-limited and capped by a cost budget.
- **Input:** a compact state summary, relevant knowledge-base entries, all clue text, the current plan, and the operator's directives.
- **Its main job** is interpretation: turn clue text (riddles and directions) into concrete goals, such as which entrance mark matches a clue, what tool an entrance needs, or which odd block to try.
- **Output:** JSON checked against a schema. The example uses an invented world:

  ```json
  {"goals": [{"op": "buy", "code": "torch", "why": "clue: the cave is dark"},
             {"op": "travel", "to": "entrance", "x": 120, "y": 40},
             {"op": "break_block", "x": 118, "y": 41, "capability": "burn"}],
   "params": {"fight_margin": 1.5, "retreat_health": 0.5},
   "notes": "The sign says the door is behind the burnt hedge.",
   "say": {"npc_type": "guard", "text": "hello"}}
  ```

- **Operations** form a fixed set: `travel`, `explore_area`, `read`, `say`, `buy`, `break_block`, `use_block`, `compose`, `fetch_item`, `gather_gems`, `hunt`, `enter_level`, `fight_boss`, `avoid`, `wait`, `set_param`. Each maps onto a state. Anything outside the set is dropped and logged.
- **Packaging:** an optional extra, so the core stays standard library only. With no model configured, the plan comes from the character file and simple built-in rules: read everything, gear up, hunt, explore entrance marks.

### Runtime directives

A per-character file, `characters/<name>.directives.toml`, re-read whenever it changes:

```toml
params = { retreat_health = 0.5, fight_margin = 2.0 }   # applied directly, no LLM
goals = ["gather_gems:20", "buy:bronze_mail"]           # replaces the goal stack
instructions = """
Do not attack other players' characters.
If you find a sign with numbers on it, try them as a code at the nearest locked door.
"""                                                     # free text, passed to the strategist
```

Structured keys take effect on the next tick with no model involved. Free text only steers the strategist.

## Milestones

| # | Milestone | Done when |
|---|---|---|
| 0 | **Discovery.** Docs read, hand play through MCP, [GAME_NOTES.md](GAME_NOTES.md) written. | Done: every unknown above has an answer or a measurement to take |
| 1 | **Executor.** `Step`/`Wait` pacing, multi-intent queues, two poll cadences, deltas and complete snapshots, health tracking. A live smoke test against Olympuff. | A character walks 200 blocks with no `movement_cooldown` rejections, using under a quarter of its request budget while calm |
| 2 | **State machine and survival.** Replace `brain.decide` with prioritised states: `Sync`, `Downed`, `Escape`, `Retreat`, `Heal`, `Flee`, `Recover`, `Explore`. Cost-grid planner, rejection learning, stuck detection and escalation steps 1, 3 and 5. Trace replay tests and the navigation fixtures. | Survives an hour in the overworld, retreating before the next two hits could kill, healing from food, and recovering its chest only when the spot is safe; it measures whether health returns in safe zones; reaches a point 150 blocks away through fog and obstacles, or gives up with a reason, never loops |
| 3 | **Gear, economy and combat.** `Gather`, `Shop`, `Loot`, `Equip`, `Fight` with group-aware win estimates and the retreat queued; learned threat and item tables. | Earns gems, buys armor, a weapon and a potion reserve, and kills lone weak hostiles without dying; never starts a fight below its health floor |
| 4 | **Navigation and knowledge.** Per-world knowledge base, overworld `Travel` to entrance marks and back to town, door graph and cross-map routing, per-block break memory, `Break` (escalation steps 2 and 4). | Visits every entrance mark within its strength, records what each needs, and returns to town |
| 5 | **Curiosity and clues.** Interest list, odd-block detector, `Investigate` and `Break` under the curiosity budget, clue capture with place and time, no-LLM clue rules. | Every readable cell and NPC within 25 blocks of its route has been read or spoken to; it finds and opens an odd block in a test map, and never spends a tool twice on the same block |
| 6 | **Strategist and directives.** LLM planner thread, plan schema, directives file, trace logging. | Given clues from a test world, it plans the right `buy`/`travel`/`break_block` operations and the state machine carries them out |
| 7 | **Levels.** `Level`, `Boss`, `Solve` (`Compose`, keys at doors). Boss preconditions from the plan; boss progress from its `health`. | Clears the easiest open level unattended, then uses what it learned to attempt the next |
| 8 | **Evaluation.** Metrics per run (levels cleared, deaths, kills, gems, time per level), compared across commits. | A regression shows up as a number |

Milestones 1 and 2 come first whatever else changes. Milestones 3 to 7 now have known shapes. What remains uncertain is the order of levels in each world, and the strategist learns that.

## Risks

- **The early game is lethal.** Ten health against hostiles that gang up means one bad engagement costs a life. Lives are finite on live worlds (Pippin is down to 5). Mitigation: conservative defaults, an escape queued with every attack, and practice on the sandbox before Olympuff.
- **Latency kills.** Hand play through MCP lost a life to a 4 s round trip. The executor must poll every tick while threatened; this is a milestone 1 requirement, not a tuning detail.
- **Clue interpretation is the hard part.** Riddles and directions are written for people. Without the strategist the agent can still gear up, hunt and walk to entrances, but it will not know what a locked or hidden entrance wants. Keep the no-LLM path useful; accept that levels need the strategist.
- **Spoilers.** Real-world clue text must never reach the repo, the tests or the PR text. Fixtures use invented worlds; the knowledge base stays in gitignored `.state/`.
- **Getting stuck.** Fog, regrowing blocks and NPCs in corridors will trap a naive walker. The Navigation section makes "stuck" a detected state with fixed escalation and a give-up, never a silent loop.
- **LLM cost and latency.** Strict triggers and a budget cap; the agent must play acceptably with the strategist off.
- **Lives are finite.** The risk level follows the lives left, and a floor stops fights and deep exploration near the end. Losing a life is logged as a decision to review, not just a counter going down.
- **Learned stats are noisy early.** Keep conservative defaults (flee more, fight less) until the tables have samples.
