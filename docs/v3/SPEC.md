# v3 agent: plan

Status: plan for Evan's review. Nothing is built to it yet. The draft code under `python/adk3/` and `python/lou/` came before this plan and is reworked to it, not kept as is.

Sources read in full before writing this: the Manual (all sixteen sections), the API rules, Create a Character Agent, the State machine guide, the Changelog, and `docs/GAME_NOTES.md`.

## What went wrong so far

Gary (v1) is rules grown case by case. Lorraine (v2) was meant to be a system and drifted back into cases. The first v3 draft wandered for a few minutes and died. One cause runs under all three: **the agent does not reason about what it needs.** Gary acts on rules, Lorraine walks a fixed tree of hand-written readiness formulas, and the v3 draft handed a blind LLM a list of primitive tasks. None of them can answer "why am I doing this, and what would make it unnecessary?"

The other causes:

- **The planner is blind.** It saw what was in view, not what the character knows: no map, no memory of shops, gem fields, hostiles, entrances, deaths, clues.
- **No middle layer.** Nothing broke "beat level 1" into steps, so the LLM had to plan in coordinates.
- **Danger was a threshold.** The controller walked into a snotling and died with every option scoring zero.
- **Built wide before deep.** Ten task kinds before one goal worked end to end.

## The idea

The agent works backwards from the objective, the way a person would:

- What is needed to beat the game? Clear every level.
- What is needed to clear a level? Reach its boss, and beat the boss in time.
- What is needed to reach it? Know where the entrance is, get through the door (a key, a bomb for a hidden door, a raft for water), and survive the level's hostiles and traps.
- What is needed to beat the boss? Enough damage, defense and healing for that boss: gear and potions.
- What is needed to get gear? Usually gems and a seller who has it; sometimes a find or a boss drop.
- What is needed to get gems? Field work, kills, gem piles and caches.
- Anything not known yet (where the entrance is, what the boss hits for, which shop sells what) is a need too: knowledge, got by exploring, talking, reading or scouting.

The agent holds this as a **needs graph**: what the objective requires, down to what can be done now. Each need is evaluated against what the agent knows: **met**, **unmet**, or **unknown**. The first unmet or unknown need on the cheapest path to the objective is what to work on.

**Nothing in it names a specific item or place.** The kinds of need come from the game's rules (the Manual): levels have entrances and bosses, doors can be locked or hidden, fights are decided by damage against health, gear is bought with gems. Which gear is right for a level, which level to take first, what a clue means, and how much readiness is enough are judgments. **That is where the strategist comes in.**

## Architecture

```
  perception ─► Memory (world knowledge, kept across runs)
                  │
                  ▼
            Needs graph ◄──── Strategist (LLM): judgments and choices
            met / unmet / unknown, with evidence
                  │  the need to work on now
                  ▼
             Executive: picks and runs a Skill for that need
                  │  the skill's current target
                  ▼
             Controller (every tick): the best action now,
             with survival, fights, healing and pickups scored on one scale
                  │
                  ▼
               intents
```

### 1. Memory

Everything the character has learned, kept per world across runs. This is the world model the strategist reasons over. The game serves only what is in view, so memory is the agent's job (Manual §12: "keep your own world model").

- **Map:** every tile seen, per map; doors and where they lead; safe ground; regions.
- **Entrances:** every minimap mark; for each, reached or not, locked, hidden, across water, which level it opened.
- **Levels:** maps inside, boss type and health seen, the time limit learned, how far each attempt got and how it ended.
- **Shops:** where each priced supply was seen, at what price, when.
- **Income:** gem yield per region from our own cuts, gem piles and when they return, oddities and caches.
- **Hostiles:** per type, the hits measured, health from our swings until a kill, where seen, how they moved.
- **Clues:** every sign, scroll and helper line, with where and who.
- **Deaths:** where, what killed us, and where the chest landed.

The ADK's knowledge base and world model already hold much of this; v3 reuses their storage and adds what is missing.

### 2. The needs graph

A small set of need kinds, each with how it is evaluated from memory and what can satisfy it. The kinds are the game's rules, so they are code; their instances and the judgments in them are data.

| Need | Met when | Satisfied by |
|---|---|---|
| Objective | every level cleared | one Clear(L) per level |
| Clear(L) | L in `levels_cleared` | Reach(L) and Beat(L) |
| Reach(L) | we can stand at L's boss door | Know(entrance of L), Pass(door), Survive(L's maps) |
| Pass(door) | the door warps us | a key held, the hidden block broken (a capability), water fordable (a capability) |
| Beat(L) | our expected fight against L's boss is a win inside its time limit | Power ≥ need, Heals ≥ need |
| Power, Heals | our gear and potions meet the level a judgment set | Have(item) for the items chosen |
| Have(item) | carried, armed or worn | Buy (Gems ≥ price and a known seller), Find, Recover (our death chest) |
| Gems(n) | gems ≥ n | income skills |
| Know(fact) | memory holds it | explore, talk, read, scout |
| Alive | health and lives allow the next step | heal, retreat |

Every evaluation that needs a number nobody serves (how much power level 3 needs, what a boss hits for) starts as the **strategist's estimate**, marked as such, and is replaced by **evidence** as it arrives: a boss's health seen at the door, the hits a hostile landed, an attempt that failed at 40% of the boss's health. A failed attempt is information, not a stall.

### 3. Strategist (LLM)

The strategist reads the needs graph with its status and evidence, the memory summary (map regions, entrances, shops seen, income, hostiles, clues, deaths) and the game reference, and answers:

- **Judgments:** what each unknown is likely to be ("level 2's boss likely needs iron-tier damage"; "this rumor points at the north-east entrance"), each with its reason and confidence.
- **Choice:** which path through the graph to take now ("level 1 first: its entrance is nearest and the rumor says it is the easiest"), and which item satisfies a need ("bronze sword and bronze mail: within reach of 8 minutes of field work").
- **Focus:** the need to work on.

It is asked when the focus need is met or fails, on a surprise (death, new map, level cleared, a clue), when evidence contradicts one of its judgments, and on a slow timer. Between answers the agent keeps working the last focus. It never picks steps or targets cells.

### 4. Executive and skills

A **skill** is a reusable procedure that satisfies one kind of need: travel to a place (across maps and doors), explore toward something, earn gems, buy, equip, talk, read, scout a level, fight a boss (with its clock), recover the death chest. Each has preconditions, a target, a done test, a progress measure and failure reasons. The executive runs the skill for the focus need. A skill that fails reports why, as evidence on that need.

Skills are the only place game procedures live, and each is small. A new behavior is a new skill or a new need kind, never a branch inside another.

### 5. Controller

Every tick it scores the actions open now (continue the skill's walk, step to safety, swing, drink, pick up) and sends the best. Danger is one model, from the Manual's rules: hostiles chase at our pace and stop at walls, swing on their cooldown when in reach, and deal nothing on safe ground; a fight ends when one side dies. Each action is scored by our expected health after it plays out, so walking on under attack, fleeing, fighting and drinking compare on one scale. The strategist sets one knob, risk, from the need's value.

Navigation replans every decision from what is known, with unseen ground priced as probably open, and sends only a few steps at a time, so an obstacle that comes into view is routed around on the next decision.

## The measure

**How annoyed Evan is, and how many cycles go by with no useful progress.**

- **In a run:** a progress ledger of real milestones: a level cleared, a new map entered, gear better than any held before, a capability gained (opens, cuts, fords, breaks), an entrance reached, a gem threshold. Not new ground walked. The headline numbers are minutes since the last milestone and milestones per hour.
- **No silent stalls:** a run with no milestone for a set time stops itself and writes a one-page report: the focus, the needs graph's state, what was tried, and why each failed.
- **In development:** each phase below ends with a live run whose result is checked against its done-when. A change that adds behavior shows what it bought.

## Seeing it think

A live view, readable at a glance:

- **Status line, every second:** focus need | skill (progress) | action and its score | health, gems, gear | minutes since the last milestone.
- **The needs graph** on demand: each need with met, unmet or unknown, its evidence or estimate, and the path chosen.
- **Trace:** one JSON line per decision; the strategist's input and answer kept beside it.

## Phases

Each phase ends with a live run on a fresh or reset character and is not done until that run meets its done-when.

| # | Builds | Done when |
|---|---|---|
| 1 | Memory, perception, the live view; the controller with the danger model; the travel and explore skills | From town, walks to a named minimap entrance on the overworld and back without dying, routing around fences and water |
| 2 | Earn gems (yield learned per region), buy, equip, recover the death chest | A fresh character holds a bronze sword and bronze mail, bought, within about 15 minutes, with no deaths |
| 3 | The needs graph and the strategist | From a fresh character, the strategist picks a level and the agent readies for it and reaches its entrance, every step explained in the view |
| 4 | Level skills: scout, fight the boss on its clock, leave before the limit, retry with what was learned | Level 1 cleared |
| 5 | Run it | Levels cleared in order across sessions; milestones per hour beat Lorraine's on the same world |

## Code

- Plain, explicit types: one dataclass per concept, named fields, no grab-bag `data` dicts.
- One concept per module, small modules, names a newcomer reads without the spec.
- Comments say the rule in game terms and cite the Manual where it comes from.
- Reuse the ADK's plumbing (client, pacer, world model, knowledge base storage, pathing, queue building, combat math from the Manual); import nothing of Gary's or Lorraine's decisions.
- No branch for a situation a run hit: it is a need kind, a skill, an estimate, or the strategist's call.

## Open questions for Evan

1. What in the draft code put you off? The plan above assumes the grab-bag `Action.data`, the dense one-liners and the scattered game details; say if it was something else.
2. Phase 1 reuses the ADK's world model and pathing. Keep that, or does v3 start its own?
3. Name: Layered Lou, or another.
