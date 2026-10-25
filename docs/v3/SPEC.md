# v3 agent: plan

Status: plan, agreed with Evan. Being built. The draft code under `python/adk3/` and `python/lou/` came before this plan and is reworked to it, not kept as is.

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

**The world tells you how to progress through text** (Manual §16): rumor helpers give rough directions toward level doors, waystation helpers say what locked doors need, a helper near a secret hints at it, signs and statues carry text, and an odd block out of place is often breakable. So clues are the first source of knowledge, not a side task:

- Lou reads every sign and talks to every helper she has not heard yet, whenever one is in reach and nothing more urgent is on (an opportunity, below): a `Read` costs no cooldown, a `Say` reaches 25 blocks, and the line comes back the same tick.
- Every line is kept in memory with who said it and where, and handed to the strategist, whose first job is to turn clues into judgments on the needs graph ("this rumor points at the north-east entrance"; "the locked door needs a key from the level 2 boss").

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
             Engines (every tick): the one for the situation
             (combat, boss, travel, work, clues) acts in real time
             on tactics the strategist wrote ahead
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
- **Clues:** every sign, scroll and helper line, with where and who; which helpers and signs are heard or read, and which are not yet.
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

The strategist reads the needs graph with its status and evidence, and the memory summary (map regions, entrances, shops seen, income, hostiles, every clue, deaths). Its standing context, sent once and cached, is the game's own documentation: the Manual's game rules (§11) and Playing the world (§16), and `docs/GAME_NOTES.md`. So it knows what breaks what, where gems come from and at what rate, the gear tiers and where each is sold, how hostiles and bosses behave, and how the world hints at its secrets, without any of it written into code. It answers:

- **Tactics:** the rules each engine plays by (whom to fight, when to drink, when to leave), revised when evidence says they were wrong.
- **Judgments:** what each unknown is likely to be ("level 2's boss likely needs iron-tier damage"; "this rumor points at the north-east entrance"), each with its reason and confidence.
- **Choice:** which path through the graph to take now ("level 1 first: its entrance is nearest and the rumor says it is the easiest"), and which item satisfies a need ("bronze sword and bronze mail: within reach of 8 minutes of field work").
- **Focus:** the need to work on.

It is asked when the focus need is met or fails, on a surprise (death, new map, level cleared, a clue), when evidence contradicts one of its judgments, and on a slow timer. Between answers the agent keeps working the last focus. It never picks steps or targets cells.

### 4. Executive and skills

A **skill** is a reusable procedure that satisfies one kind of need: travel to a place (across maps and doors), explore toward something, earn gems, buy, equip, talk, read, scout a level, fight a boss (with its clock), recover the death chest. Each has preconditions, a target, a done test, a progress measure and failure reasons. The executive runs the skill for the focus need. A skill that fails reports why, as evidence on that need.

Skills say what to do toward a need; engines say how to act in the moment. A new behavior is a new need kind, skill, engine or tactic, never a branch inside another.

### 5. Engines: real-time play

Every tick one **engine** runs the moment. Each is a small real-time algorithm for one kind of situation, decides without a model call, and reads **tactics** the strategist wrote ahead of time (Create a Character Agent: "ask it what to do when the fight starts ... it writes those answers down as ordinary rules"):

| Engine | Runs when | Does | Tactics it reads |
|---|---|---|---|
| Combat | a hostile threatens us | fight, back off to safe ground or behind a wall, drink, by the expected-health model below | which hostiles to fight or avoid, when to drink, when to leave |
| Boss | in a boss room | the fight on its clock; leaves by the exit door before the limit or when losing | when to leave, when to drink |
| Travel | the skill's target is a place | the path, replanned every decision as obstacles come into view; doors and warps | how much danger to walk past |
| Work | the skill works blocks or pickups | cut, break, pick up drops, move to the next block | none |
| Opportunities | nothing threatens and something in view is worth its detour | takes the best opportunity (below) | what a life, health and knowledge are worth in gems |

The situation picks the engine each tick: a threat outranks everything, then the boss room, then an opportunity worth its detour, then the engine of the skill under way.

**Opportunities** are the reactive rules: things in view the Manual says are worth stopping for. Each is priced in one unit, gems, against its detour (the extra steps, and the danger on the way, from the danger model), and the best one worth more than its detour is taken:

| Opportunity | Worth |
|---|---|
| Gems on the ground, free | their count |
| Food, when hurt; a bush to cut for berries when no food is near | the health it gives back, at what a potion costs per health |
| A sign not read, a helper not heard | knowledge: the strategist sets its worth |
| An odd block (one type alone where it does not belong, Manual §16) that a capability we carry may break | the gem cache it may hold |
| Our death chest | what it holds, at the gems each item cost |
| A heart (a life) | the strategist's worth of a life |

A new reactive rule is one row in that list, priced like the rest, never a branch elsewhere. The strategist never acts in the moment; it revises tactics between moments, when evidence says they were wrong (a death, a fight lost, a hostile stronger than estimated).

**Risk against reward** (Manual §11 Hostiles, Combat, §16). The combat engine weighs every option it has (fight on, walk on, back off to safe ground or behind a wall, drink) as reward less risk, all priced in gems, the unit the opportunities use:

- **Reward:** the skill's progress the option keeps (getting where it is going is worth the time it saves), and what a kill is worth: its drop (the Manual's kill-drop rates), the hits a dead chaser no longer lands, and for a boss, the level.
- **Cost:** the health it is expected to lose, at what potions cost per health; the chance it kills us, times what a death costs (a life, everything carried, since the chest drops, and the time to get back); and the time it takes, at our earning rate.
- **The chance of dying** comes from playing each option out many times with the dice rolled (the Manual's hit and damage rolls), hostiles chasing at our pace and stopping at walls, swinging on their cooldown, and dealing nothing on safe ground.
- **How much risk to take** is the strategist's tactic: what a death costs us now (fewer lives or more gear carried makes it dearer) and what each goal is worth.

So a fight with good odds and a small reward is taken, one with a real chance of death is not, and walking past a slow hostile is taken when the place ahead is worth the hit.

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

## Delivery

One delivery, not discovery runs. Every layer is built end to end (memory, the needs graph, the strategist, the skills, the controller, the live view) and tested offline against recorded and fake game states. The first live run is the acceptance of the whole product: a fresh character, unattended, works toward clearing levels, every step explained in the view. Iteration after that is fixes, and each fix names its home: a need kind, a skill, an engine, a tactic, an estimate, or the strategist's input.

Build order inside the delivery: memory and perception; the controller and its danger model; travel and explore; clues (read, talk); earn gems, buy, equip, recover the death chest; the needs graph; the strategist; level skills (scout, boss fight on its clock, leave before the limit); the live view and the run report.

## Code

- Plain, explicit types: one dataclass per concept, named fields, no grab-bag `data` dicts.
- One concept per module, small modules, names a newcomer reads without the spec.
- Comments say the rule in game terms and cite the Manual where it comes from.
- Reuse the ADK's plumbing (client, pacer, world model, knowledge base storage, pathing, queue building, combat math from the Manual); import nothing of Gary's or Lorraine's decisions.
- No branch for a situation a run hit: it is a need kind, a skill, an estimate, or the strategist's call.

## Decided

- Name: Layered Lou (`lou`).
- Reuse the ADK's world model and pathing where they serve; replace what does not.
