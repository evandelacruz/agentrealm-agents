# Plan: an agent that can actually play

Goal: a reference agent that survives, gears up, fights, travels the overworld, solves clues, and clears levels. "Clear all `level_count` levels" is how a world is beaten (`GET /world`); Olympuff has 8.

## Verdict on the approach

A deterministic state machine for play, plus an LLM that plans off the tick, is a good fit for this game. It is the standard hybrid of fast reactive control and slow deliberation, and the API is shaped for it. Intent queues carry up to about 4 s of actions, rate limits allow one request per tick, and the game runs at 10 Hz. So no model call can ever sit on the tick path, and none needs to.

Four rules keep it working:

1. **Use priority states, not a flat FSM.** Survival interrupts first, opportunities second, the plan's goal last. A flat graph of every state-to-state transition grows too large to maintain.
2. **The LLM never emits intents.** It emits typed plan operations from a fixed vocabulary (go here, read that, try this answer, avoid that NPC type). The state machine turns them into moves. Unknown or invalid operations are dropped.
3. **Replays stay deterministic.** Every LLM answer and directive change is written to the trace, so a run can be replayed without the model.
4. **Game knowledge is the real bottleneck, not architecture.** How levels are cleared, what `Compose` makes, and what NPCs say are not documented in this repo. Milestone 0 finds out.

## What the API offers that the agent ignores today

| API surface | Used today | Needed for |
|---|---|---|
| `Step` + `Wait` pacing (base 2.5 blocks/s means a step every 4 ticks) | No: sends a move every window, so many are rejected `movement_cooldown` | Moving at all |
| Multi-intent queues | No: always one intent | Freeing the request budget |
| Snapshot deltas (`snapshot_version`), health in the observation | No: separate entity reads, health never tracked | Perception, retreat |
| `Arm`, `Wear`, `Remove`, `Drop`, `attack_range` | No | Gear |
| `Use` on a block (attacks the NPC on it) | No: flees from every NPC | Fighting NPCs |
| `Read` (readable blocks, scrolls), `Say` to an NPC | No | Clues, puzzles |
| `Compose` (supplies into something), fragment slots, gem prices | No | Crafting, value of loot |
| `locked` blocks, traps and `Disarm`, `occupy_damage` | No | Level obstacles |
| Zones: `safe`, hunting-ground strength ceiling | No | Where to rest, where to hunt |
| Minimap: level entrance marks, map supply reveals cells | No | Overworld travel |
| `Sleep`, respawn delay, lives | Partly | Ending a session, death handling |

## Architecture

```
            ┌────────────── strategist (LLM, optional, async) ──────────────┐
 directives │ triggers: clue read, NPC spoke, new level, stuck, goal done, │
 (hot file) ┤ death. In: state summary + knowledge. Out: plan operations. │
            └──────────────────────────────┬────────────────────────────────┘
                                           ▼ validated plan (goal stack + parameters)
  knowledge base ◄──── world model ◄──── perception (tick deltas, rare terrain reads)
  (per world, on disk)      │
                            ▼
                    state machine  ── every tick: pick state by priority, state.act()
                            │
                            ▼
                    executor ── builds paced intent queues, sends one round trip,
                                re-sends only when the queue is invalidated
```

### Executor (fixes milestone 1)

- Paths become `Step`, `Wait`×n, `Step`, … queues. The number of waits comes from `movement_speed`, and the queue is cut at the world's horizon.
- One round trip carries the queue. The executor sends a new one only when a result is rejected, a delta changes something on the path, or the state machine changes state. The rest of the request budget goes to rare reads.
- Perception comes from the round trip's observation, sent with `snapshot_version`, so it arrives as deltas. With perception 25 the terrain window is 51×51, so terrain reads happen only on a map change or after a long walk.
- Health, inventory and entity changes are folded into the world model from the deltas.

### State machine

States are checked in priority order each tick. The first whose guard holds runs; each has entry and exit conditions with hysteresis so it does not flip back and forth.

| Priority | State | Enters when | Does |
|---|---|---|---|
| 0 | `Sync` | Unplaced, position unknown, after a warp | Reads self and position, waits for placement |
| 0 | `Downed` | `Died` | Waits for `Respawned`, then queues `Recover` |
| 1 | `Escape` | Standing on damage, or trapped | Steps off; crosses as little hazard as possible |
| 1 | `Retreat` | Health below the retreat threshold, or the threat is stronger than us | Goes to the nearest known safe zone, rests until the resume threshold |
| 2 | `Fight` | A hostile is in range and we are likely to win (see Combat) | Closes to `attack_range`, `Use` on the character or the NPC's block |
| 2 | `Flee` | A hostile is in range and we are likely to lose | Opens distance toward safety |
| 3 | `Recover` | Our death chest is on a reachable map | Walks there, `WithdrawFromChest` |
| 3 | `Equip` | Carrying something better than what is worn or armed | `Arm`, `Wear`, `Remove` |
| 3 | `Loot` | A worthwhile supply or chest is near enough | Walks, `Take` or `WithdrawFromChest`, `Drop` junk when full |
| 4 | `Investigate` | An unread readable block, an unread scroll, or an NPC not yet spoken to, near the route | `Read`, `Say`; stores the text as a clue |
| 4 | `Solve` | The plan holds an answer to try (a code, an item to compose, a block to use) | Carries out the plan operation and checks the result |
| 5 | `Level` | Inside a level | Runs the level objective from the plan (found in milestone 0) |
| 5 | `Travel` | The plan names a destination (entrance, town, hunting ground) | Long A* over revealed ground, through doors |
| 5 | `Explore` | Nothing else | Frontier exploration, entrances first |
| 6 | `Idle` / `Sleep` | Stopping, or the plan says wait | Sends nothing, or `Sleep` at shutdown |

Every state is a small class with `guard(world, plan) -> bool`, `act(world, plan) -> Queue | None` and `done(world) -> bool`. They are tested the way `brain.py` is tested today: a model in, an intent queue out.

### Combat

- **Our reach**: `attack_range` from `get_self`, refreshed after `Arm`. A `target_out_of_range` result also reports the reach and distance.
- **Threat by NPC type**: damage per hit and hit rate, learned from `Damaged` events (`source_kind`, `source_id`) and kept in the knowledge base. Our damage per hit is learned from target health changes in deltas.
- **Win estimate**: ticks for us to kill it versus ticks for it to kill us. Fight above a margin, flee below it. Directives can bias the margin.
- **Where to hunt**: hunting grounds advertise a strength ceiling; pick the strongest one we can win in.
- **Never fight in a safe zone** (attacks cannot be made there); use it to recover.

### Gear and items

- An item table keyed by `supply_subtype_code`, filled by observation: reach and damage after `Arm`, damage taken after `Wear`, gem price as a fallback value.
- `Equip` scores each slot and swaps when a carried item beats the worn one.
- `Compose`: recipes come from clues, scrolls or the LLM, and are confirmed by trying them. A failed attempt is recorded so it is not retried.

### Knowledge base

A JSON file per world in `.state/`, shared by every character of that world:

- Revealed terrain per map, level entrances, doors and where they lead, safe zones, hunting grounds.
- Clues: text of every sign, scroll and NPC line, with where it was found.
- NPC type stats, item stats, recipes tried and their results, puzzle answers tried and their results.
- Level progress: which levels are cleared, and the route and solution for each.

This is what makes a second run better than the first, and it is what the strategist reads.

### Strategist (LLM, optional)

- **Runs** in its own thread. The state machine keeps the old plan until a new one lands.
- **Triggers**: a new clue, an NPC reply, entering a level, no progress for N minutes, a goal finished or failed, a death. Calls are rate-limited and capped by a cost budget.
- **Input**: a compact state summary, relevant knowledge-base entries, the clue text, the current plan, and the operator's directives.
- **Output**: JSON checked against a schema.

  ```json
  {"goals": [{"op": "travel", "to": "entrance", "level": 3, "why": "sign says start at 3"},
             {"op": "use_block", "x": 412, "y": 88},
             {"op": "compose", "codes": ["fragment_a", "fragment_b"]}],
   "params": {"fight_margin": 1.5, "retreat_health": 0.4},
   "notes": "The riddle answer is probably 'kettle'; try Say to the guard.",
   "say": {"npc_type": "guard", "text": "kettle"}}
  ```

- **Operations** form a fixed set: `travel`, `explore_area`, `read`, `say`, `use_block`, `compose`, `fetch_item`, `avoid`, `hunt`, `wait`, `set_param`. Each maps onto a state. Anything outside the set is dropped and logged.
- **Packaging**: an optional extra, so the core stays standard library only. With no model configured, the plan comes from the character file, as it does today.

### Runtime directives

A per-character file, `characters/<name>.directives.toml`, re-read whenever it changes:

```toml
params = { retreat_health = 0.5, fight_margin = 2.0 }   # applied directly, no LLM
goals = ["hunt:moose_hills", "level:1"]                 # replaces the goal stack
instructions = """
Prefer swords over bows. Do not attack other players' characters.
If you find a sign with numbers on it, try them as a code at the nearest locked door.
"""                                                     # free text, passed to the strategist
```

Structured keys take effect on the next tick with no model involved. Free text only steers the strategist.

## Milestones

| # | Milestone | Done when |
|---|---|---|
| 0 | **Discovery.** Play by hand through the MCP tools and write `docs/GAME_NOTES.md`: how a level is entered and cleared, combat numbers, item kinds, `Compose`, what NPCs answer to `Say`, signs, locks and keys, traps. | Each unknown in this plan has an answer or a test to find it |
| 1 | **Executor.** `Step`/`Wait` pacing, multi-intent queues, snapshot deltas, health tracking. A live smoke test against Olympuff. | A character walks 200 blocks with no `movement_cooldown` rejections, using under a quarter of its request budget |
| 2 | **State machine and survival.** Replace `brain.decide` with prioritised states: `Sync`, `Downed`, `Escape`, `Retreat`, `Flee`, `Recover`, `Explore`. Trace replay tests. | Survives an hour in the overworld, retreating and recovering on its own |
| 3 | **Gear and combat.** `Loot`, `Equip`, `Fight`; NPC attacks through `Use` on a block; learned threat and item tables. | Kills NPCs in the weakest hunting ground and equips what it finds |
| 4 | **Navigation and knowledge.** Per-world knowledge base, overworld `Travel` to entrances and back to town, door graph. | Walks from town to any known entrance and back |
| 5 | **Clues.** `Investigate`: `Read`, `Say`, clue capture. | Every sign and NPC near its route is in the knowledge base |
| 6 | **Strategist and directives.** LLM planner thread, plan schema, directives file, trace logging. | Given a planted clue, it plans the right operation and the state machine carries it out |
| 7 | **Levels.** `Level` and `Solve` from milestone 0's findings. | Clears level 1 unattended, then more |
| 8 | **Evaluation.** Metrics per run (levels cleared, deaths, kills, time per level), compared across commits. | A regression shows up as a number |

Milestones 1 and 2 are worth doing whatever milestone 0 finds. Milestones 3 to 7 change shape depending on it.

## Risks

- **Level mechanics may need more than the API shows.** If a level needs something no verb does, that is a server gap and goes in PLAN.md, not a workaround.
- **LLM cost and latency.** Strict triggers and a budget cap; the agent must play acceptably with the strategist off.
- **Learned stats are noisy early.** Keep conservative defaults (flee more, fight less) until the tables have samples.
- **"Build" is unclear.** No verb places blocks. `Compose` may be the crafting the game means; milestone 0 confirms.
