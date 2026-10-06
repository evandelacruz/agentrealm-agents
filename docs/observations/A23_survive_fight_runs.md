# A23 live survive-a-fight runs with the planner on (redacted)

Scenario: survive a fight. `scripts/smoke_m8_olympuff.py --seconds 300 --profile <copy>` with the AI planner on, on a copy of `python/characters/olympuff_m8.toml` (`on_hostile = "fight"`, `hostile = ["npc"]`) whose directives file holds one goal, `goals = ["travel:hunting_ground"]`, the only directive that sends the character toward hostile ground. Survival params at their defaults and floors (`fight_margin` 1.5, `retreat_hits` 2, `risk` 0.5, `lives_floor` 3). The M8 smoke is used for its fight metrics (fights below the health floor, heals, weak kills). Character chosen at run time; no name or id is committed. Survival judged only.

## Run 1 — FAIL: death at 274 s; one fight, lost, then a Retreat that could not get clear

- **Code:** `main` at `7f9c795` (after #119 and #120). Planner: Anthropic, default model. Knowledge base from Walk run 4 earlier the same session (map 76 terrain, no hunting-ground facts).
- **Verdict:** exit 1 after **274 s** (first death ends the run). Character started at 10/10 health, 10 lives, 0 gems, pocket knife armed, no potions or food, standing in the town safe zone at (381, 377).
- **Gate summary:** deaths **1**; API errors **0**; fights below the health floor **0**; heal food take and heal potion both **no**; weak hostile kills 0; gems earned no.

### Fights

| | |
|---|---|
| Fights engaged | **1** (gristlewick 217, at 8/10): 2 `Use` swings, 1 `NPCAttacked`, **0** `NPCDamaged` |
| Won | **0** |
| Fled | 2 episodes, 5 decisions: farmer npc 126 at start (2), gristlewick 217 (3) |
| Retreated | 1 episode, **14** decisions, all `retreat → safe (399, 370)`; never arrived |
| Heals used | **0** (nothing to eat or drink; Heal does not run with a hostile in range) |
| Deaths | **1** (lives 10 → 9) |
| Fought below the health floor | **no** (the floor is 4: `retreat_hits` 2 × a hit of 2; the only swings went out at 8/10) |

### Health curve

| Time | Tick | Health | What |
|---|---|---|---|
| 0–258 s | — | 10/10 | town and its edge, explore |
| 258 s | 3958626 | 10/10 | `Attacked` by 217, missed |
| 260 s | 3958644 | **8**/10 | hit 2; Flee, then fights back |
| 262 s | 3958673 | **7**/10 | hit 1; Retreat starts |
| 267 s | 3958720 | **5**/10 | hit 2 |
| 269 s | 3958736 | **3**/10 | hit 2 |
| 272 s | 3958768 | **1**/10 | hit 2 |
| 273 s | 3958783 | **0** | hit 1, `Died` |

258 s at full health, then 10 → 0 in **16 s** against one gristlewick. 217 attacked 11 times in that time; 6 landed.

### Planner

| | |
|---|---|
| Calls | 22 (7 `applied`, 13 `unchanged`, 2 `kept` with no goals) |
| Plans accepted / errors | **20 / 0** |
| Tokens | input 11,785, output 3,350, cache write 0 (the prefix was still cached from Walk run 4), cache read 2,157,188 |

The planner kept `explore_area` around town, then `gather_gems` ×15, for 250 s. The hurt trigger at 3/10 (call 22) answered with `travel to town`, `wait 30`, `retreat_hits: 1`, `fight_margin: 2.0`; see defect 3.

### Decision mix (67 decisions that sent intents; 118 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| explore_area (planner) | 40 |
| retreat | 14 |
| explore (safe default, while the pinned travel had nowhere to go) | 6 |
| flee | 5 |
| `not outrunning npc 217: fight npc 217` | 2 |
| travel (pinned `travel:hunting_ground`) | **0** |

Intents: 430 `Step`, 1,097 `Wait`, 2 `Use`. Call mix: 337 `zone`, 185 `tick`, 44 `strategist`, 25 `terrain`, 22 `position`, 11 `self`, 11 `entities`, 1 `world`. 318 of the 337 zone reads came back safe, so the town zone was well known.

### Top 3 defects

1. **Retreat cannot outrun a gristlewick, and its route bends away from the safe tile.** Retreat began at 7/10, 19 cells (Chebyshev) from the safe tile at (399, 370). It sends one `Step` per decision, and its hostile-aware cost path went south-east (x 405 → 413) around the chaser, so after 11 s and 14 steps it was still 14 cells out. 217 landed 4 more hits on the way.

   ```
   t=3958675 @76:405,351 tick  Step(down_right) (retreat → safe (399, 370)) | Attacked, Damaged 1 by npc
   t=3958723 @76:409,356 tick  Step(right) (retreat → safe (399, 370)) | Attacked, Damaged 2 by npc
   t=3958740 @76:411,357 tick  Step(down_right) (retreat → safe (399, 370)) | Attacked, Damaged 2 by npc
   t=3958770 @76:412,361 tick  Step(down) (retreat → safe (399, 370)) | Attacked, Damaged 2 by npc
   t=3958785 @? tick           Step(down_left) (retreat → safe (399, 370)) | Attacked, Damaged 1 by npc, Died
   ```

   Suspects: `states/retreat.py:62–70` (one `set_position` step per decision, and the cost path weighs the chaser so it detours instead of taking the shortest way in); `states/flee.py:97–104` chose to fight at 8/10 with `cornered` or `wins` true against a hostile the knife never damaged. A retreat that is losing ground (gap to safety not shrinking, hits landing) has no fallback. Same gristlewick, same patch (around (401–413, 351–366)) as Walk run 4's death.

2. **`travel:hunting_ground` had nowhere to go: no hunting cell was known or read, and nothing goes looking for one.** `get_zone` returns `strength_ceiling` only for a hunting-ground cell (GAME_NOTES **Zone fields**, PLAN.md API table). This run read zones only in and around town (318 of 337 safe), and the knowledge base held no hunting cells, so `_resolve_hunting` had no candidates. Travel sent no move, the safe default explored, and plan stall dropped the pinned op at 37 s. The fight came from the planner's `explore_area` radius 40 running out of town, not from the directive. This run does not show whether Olympuff has hunting grounds or whether `get_zone` reports them; it is not a server gap.

   ```
   t=3956139 @76:383,377 tick  queue 10×Step 27×Wait (explore → (406, 375))
   plan: dropped op {'op': 'travel', 'to': 'hunting_ground', 'x': 0, 'y': 0}: Travel: no progress for 30s
   ```

   Suspects: `travel/resolve.py:75–95` resolves only from cells already known (knowledge-base hunting cells, zone facts with a `strength_ceiling`), and no state or zone probe searches for an unknown hunting ground, so a `hunting_ground` op with none known just stalls for 30 s and drops.

3. **The planner's hurt reply was half rejected and half wrong.** At 3/10 the planner sent `travel to: town` without `x`/`y`; `plan.py:110–111` requires `x` and `y` for every travel op, although `town` takes none (`travel/ops.py:35`, and directives shorthand fills in 0, 0), so the op was dropped. That left `wait 30` on top, with the reason "hostiles cannot hurt in town" while the character stood 14 cells outside it with 217 adjacent. It also asked for `retreat_hits: 1` "to retreat sooner" again, rejected by `plan.py:456` (third run with that misreading).

   ```
   plan: dropped op {'op': 'travel', 'to': 'town', 'why': 'health 3/10, no potions or gems; retreat to the safe zone before doing anything else'}: missing `x`
   plan: retreat_hits 1 below floor 2; ignored
   ```

   Suspects: `plan.py:110` (`_validate_travel`), and `strategist.py`'s State, which gives `pos` but not whether the character stands on safe ground or how far it is to the nearest known safe tile.

**Minor:**
- Flee ran from the town farmer (npc 126) at the start, inside the safe zone: `hostile = ["npc"]` makes every NPC hostile.
- Call 21 carried the same `explore_area` `goal_done` trigger 8 times.
