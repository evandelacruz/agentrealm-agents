# v3 agent: plan

Status: revision 2, for Evan's review; Lou is rebuilt to it once read. Revision 1 is built (`python/adk3/`, `python/lou/`) and was run live five times; what those runs taught is below, and this revision is what changes because of it.

Sources read in full before writing this: the Manual (all sixteen sections), the API rules, Create a Character Agent, the State machine guide, the Changelog, and `docs/GAME_NOTES.md`.

## What went wrong so far

Gary (v1) is rules grown case by case. Lorraine (v2) was meant to be a system and drifted back into cases. The first v3 draft wandered and died with a blind planner.

Revision 1 of v3 fixed the blindness: its strategist read the Manual and every clue and reasoned well (it mapped the eight entrances to the gods' signs from the clues alone). But code still made strategy calls, and the runs show where that went wrong:

- **Two owners of strategy.** A needs graph in code chose the goal and the skill chose where to do it; the strategist only advised. In run 5 the strategist said "farm the fields farther out"; the code kept picking the nearest grass, which a snotling held.
- **Reactions forgot.** Combat backed Lou off from the snotling; the skill, not knowing why, sent her straight back. Neither told the other what had happened.
- **Reactions never finished.** Fight or back off was decided afresh every tick, so close calls flipped back and forth.

## The idea

**The strategist owns strategy. Algorithms carry it out.**

The strategist knows the overall goal (beat the game) and works backwards through what it needs, the way a person would: beat the game ← clear the levels ← clear this level ← carry the gear it needs ← hold the gems the gear costs ← cut grass for them. It publishes the **first dependency** as the current **goal**, with the **approach** to it and a **plan B**. The algorithms carry that out in real time without asking again, react to the moment by fixed rules, and report back. The strategist then publishes the next goal.

What is needed to beat the game, by the Manual, which the strategist reads:

- Clear every level (§3). A level is behind an entrance door, which may be locked (a key), hidden (a bomb) or across water (a raft) (§11).
- Clearing it means beating its boss inside, on a clock (§11 Bosses and levels).
- Fights are decided by gear and health (§11 Combat); gear is bought with gems where it is sold (§11 Shops, §16).
- Gems come from field work, kills, gem piles and caches (§16).
- The world tells you how to progress through text (§16): rumor helpers point at level doors, waystation helpers say what locked doors need, signs and statues carry clues, and an odd block out of place is often breakable. Lou reads every sign and greets every helper she passes, and every line goes to the strategist.

## Architecture

```
  perception ─► Memory (everything learned, kept across runs)
                  │
                  ▼
             Strategist (LLM) ── publishes ──► goal, approach, plan B, next
                  ▲                                │
                  │ done / failed (why) / surprise  ▼
                  └──────────────────────── Executive: runs the approach's method
                                                   │ its target this tick
                                                   ▼
                                     Reactions, fixed priority (every tick):
                                     1 threatened → combat (engagement plan)
                                     2 boss room  → boss fight on its clock
                                     3 worth a detour in view → opportunity
                                     4 otherwise  → the approach's method
                                                   │
                                                   ▼
                                                intents
```

### 1. Memory

Everything Lou has learned, kept per world across runs, and handed to the strategist as a summary (never raw tiles):

- **Map:** every tile seen, per map; doors and where they lead; safe ground.
- **Entrances:** every minimap mark; reached or not, locked, hidden, refused, which level it opened.
- **Levels:** maps inside, boss type and health seen, each attempt and how it ended.
- **Shops:** where each priced supply was seen, at what price.
- **Income:** gems per cut by region, gems per kill by hostile type, from Lou's own work.
- **Hostiles:** per type, hits measured, the ground it keeps to (learned from where it stops following), where each was last seen.
- **Clues:** every sign, scroll and helper line, with who and where; which helpers and signs are not yet heard or read.
- **Deaths:** where, what killed her, where the chest landed and what it holds.
- **What just happened:** each reaction's outcome (backed off from a gristlewick pair at the field by 400,440), so the approach and the strategist know why.

### 2. The strategist: goal, approach, plan B, next

The strategist's standing context, sent once and cached, is the Manual's world, game rules and play guide (§3, §11, §16) and `docs/GAME_NOTES.md`. Each time it is asked it reads Lou's state, the memory summary, the chain of needs from the overall goal down (what is met, what is not), and every outcome since it last spoke. It answers with one published goal:

```
goal:     hold 15 gems                          done when: gems ≥ 15
why:      bronze sword (15) for the north-west level, the first we can clear
approach: earn gems: cut grass in the field around 400,440
          fails if: no way there, the field is held, no gem in 3 minutes
          tactics: fight snotlings down to 3 health; avoid gristlewicks in pairs;
                   detours: gems, food when hurt, signs and helpers within 10 blocks
plan B:   earn gems: cut grass in the field around 360,450, same tactics
next:     buy the bronze sword at the town shop, 412,398
```

- **The goal** is one dependency on the way to beating the game, with a test the algorithms can check.
- **The approach** is a method from a fixed menu with its settings, its failure conditions, and the **tactics** the reactions play while it runs.
- **Plan B** is a second approach to the same goal, taken at once if the first fails, without waiting for the strategist.
- **Next** is the goal after this one, started at once when this one is met, while the strategist plans what follows.

It is asked when a goal is met or both plans fail, on a surprise (a death, a new map, a level cleared, a new clue, a hostile the tactics did not name), and on a slow timer. Lou never waits for it: she works the current approach, then plan B, then next, and with nothing left she does safe work near town (eat if hurt, cut grass) until it answers.

### 3. The executive and the methods

The executive runs the approach's method and checks the goal's test and the approach's failure conditions every decision. A method is a small procedure with settings; the menu:

| Method | Settings | Does |
|---|---|---|
| earn gems | a field | cut grass there, pick up the drops |
| buy | item, shop | walk up to the shelf, take it |
| equip | item | arm or wear it |
| travel | place, through its door or not | walk there, across maps by known doors |
| explore | toward a place, looking for what | walk to unseen ground that way until it is found |
| break open | block, item | arm the item, use it on the block |
| forage | | eat food in view, cut bushes for berries, look toward town |
| recover | our death chest | walk back, take everything that fits |
| find the boss | level | inside the level, take doors not taken and explore until the boss is in view |
| fight the boss | level | the boss fight on its clock |

When the goal's test passes the executive reports done; when a failure condition holds it reports failed with why, and switches to plan B. A new behavior is a new method on the menu.

### 4. Reactions

Every tick, in fixed priority, the first that applies acts. None calls a model; each plays the approach's tactics and the levers.

1. **Combat**, when a hostile can reach Lou. It weighs fight, back off to safe ground or behind a wall, and drink, as reward against risk (below), and once it picks, it **commits** by an **engagement plan**:
   - **Fight:** fight to the end, unless health falls to the **bar**: the tactics' bar for that hostile type, else the `heal_below` lever. At the bar she drinks if she carries a potion and fights on; with none she breaks off to safe ground, forages, and reports.
   - **Back off:** go until clear (no hostile can reach her), then report what held the ground, so the approach treats it as a failure condition and plan B or the strategist moves her elsewhere.
   - It is decided again only when the engagement ends or the bar is reached, never tick by tick.
2. **Boss**, in a boss room: fight while it pays, drink at the bar, and leave by the exit door before the clock runs out or when losing.
3. **Opportunities**, nothing threatening and something in view worth its detour, by the approach's detour tactics: gems, food when hurt (berries from a bush when no food is near), a sign not read, a helper not heard, an odd block a weapon may break, a heart, the death chest. Each is priced in gems against its extra steps and the danger on the way.
4. **The method**, otherwise: walk the path, replanned every decision, breaking bushes and trees in the way when a weapon can; cut; take; go through doors; explore.

**Risk against reward** (Manual §11 Hostiles, Combat, §16). Every option is priced in gems:

- **Reward:** the progress it keeps on the approach (a fight credited with the progress after the kill), and what a kill drops (learned per type, the Manual's rate before any kills).
- **Cost:** health it is expected to lose, at what potions cost per health; the chance it kills her, times what a death costs (a life and everything carried); the time it takes.
- **The odds** come from playing the option out many times with the Manual's dice: hostiles chase at her pace, stop at walls and at the edge of the ground their type keeps to, swing on their cooldown, and deal nothing on safe ground. One set of dice per engagement, so options are compared fairly.
- **Close calls are rolled.** When two options are close, the choice is a roll weighted toward the better one, and leaning bolder as `risk` rises, rather than always the strict best: no exact tie can loop, and Lou is less predictable. Each roll is logged with its odds.

## Levers

An agent's temperament: a few numbers in its profile (`[levers]` in `characters/lou.toml`), each feeding one price that already exists (`adk3/realm/value.py`). Two agents on this kit differ in their levers, not their code. The strategist sees them and works within them; it never changes them.

| Lever | Feeds | Lou |
|---|---|---|
| `risk` (0 to 10) | how heavily a chance of death weighs; how bold close calls roll | 5 |
| `life_worth` (gems) | what a death costs, besides the gear it drops | 40 |
| `time_worth` (gems a minute) | what time is worth, so detours and backing off cost something | 6 |
| `curiosity` (0 to 10) | what a sign, a helper or unseen ground is worth stopping for | 5 |
| `greed` (0 to 10) | what loot in view is worth stopping for | 5 |
| `heal_below` (share of health) | the bar where she drinks or breaks off, when the tactics set none | 0.4 |
| `patience` (seconds) | how long a method may make no progress before it has failed | 60 |

## The measure

**How annoyed Evan is, and how many cycles go by with no useful progress.**

- **In a run:** a progress ledger of real milestones: a level cleared, a new map entered, gear better than any held before, an entrance reached, a clue learned, a gem threshold. Not new ground walked. The headline numbers are minutes since the last milestone and milestones per hour.
- **No silent stalls:** a run with no milestone for a set time stops itself and writes a one-page report: the goals published, the approaches tried, and why each failed.

## Seeing it think

- **Status line, every second:** goal | approach (plan A or B) | reaction and its action | health, gems, gear | minutes since the last milestone.
- **The strategist's answers** printed as they land: goal, why, approach, plan B, next.
- **Trace:** one JSON line per decision; the strategist's input and answer kept beside it.

## Code

- Plain, explicit types: one dataclass per concept, named fields.
- One concept per module, small modules, names a newcomer reads without the spec.
- Comments say the rule in game terms and cite the Manual.
- Reuse the ADK's plumbing; import nothing of Gary's or Lorraine's decisions.
- No branch for a situation a run hit. A fix lives in one place: memory, the strategist's input, a method, a reaction, a tactic, or a lever.

## What changes from revision 1

- **Out:** the needs graph as the chooser of goals (its chain of needs stays, as part of what the strategist reads); judgments as separate fields (`order`, `needs`, `where`, `doors`); the skill choosing where to work on its own.
- **In:** the published goal, approach, plan B and next; failure conditions set by the strategist; the engagement plan with its bar; reactions reporting outcomes to memory; weighted rolls on close calls.
- **Kept:** memory, perception and acting, the danger model, travel (with breaking bushes and trees), the reactions' fixed priority, opportunities, foraging, territory learning, levers, the ledger, the strategist's standing context.

## Decided

- Name: Layered Lou (`lou`).
- Reuse the ADK's world model and pathing where they serve; replace what does not.
