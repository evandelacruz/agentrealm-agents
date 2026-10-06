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

## Run 2 — PASS (survival only): no deaths, no fights; the hunting-ground search never read a zone outside town, and decisions stalled the loop for half the run

- **Code:** `main` at `8966752` (after #123, #125 and #127). Server redeployed with B133 and sim changes just before the run. Planner: Anthropic, default model. **Fresh knowledge base** (new container: no terrain, zones or hunting cells carried over), unlike Run 1.
- **Verdict:** exit 0 after **302 s**. Character (an existing one, picked at run time; this was the character the coordinating session had named for these runs, and its notice arrived after the run) started at 10/10 health, 10 lives, 0 gems, pocket knife, no potions or food, at (370, 399), 27 cells west of the town cell (397, 401) and adjacent to gristlewick 240.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor **0**; heal food take and heal potion both **no**; weak hostile kills 0; gems earned no.
- **Scenario not met:** nothing fought, so this run does not satisfy survive-a-fight. It covers the hunting-ground search and the survival reflexes only.
- A second, 90 s run under `cProfile` followed on the same character to find where the main thread spent its time (defect 2). Its numbers are kept apart below and are not in the tables.
- **Lives between the runs:** Run 2 ended at 13:54:44 UTC at (358, 519) with 10 lives. The profiling run started at 13:57:57 at (396, 610) with **9** lives and 10/10 health. So the character died and moved between the two runs, while this session was not playing it. That death is in neither run's trace, and the profiling run had no death (9 lives at its last `self` read, 13:59:08). By 14:05 the character was down to 8 lives, with its last damage at 14:05:04, after both runs had ended. Another client was playing it.

### Fights

| | |
|---|---|
| Fights engaged | **0** (no `Use`, no `Attacked`, no `Damaged`) |
| Won | **0** |
| Fled | 1 episode, 3 decisions, all from townsfolk: rumor_teller 135 (2), apothecary 130 (1) at (424, 400) |
| Retreated | 3 episodes, 3 decisions (one walk queue each), all started at **10/10** from `would_lose` (the planner had raised `fight_margin` to 2.0): gristlewick 240 at spawn, snotlings 215/216 and gristlewick 217 at (401, 370), salvager 160 at (391, 504) |
| Heals used | **0** (never hurt; one apple, supply 62581, picked up at 172 s by `pickup`) |
| Deaths | **0** (lives 10 → 10) |
| Fought below the health floor | **no** (no swings; the floor rose to 6 once the planner set `retreat_hits` 3) |

### Health curve

| Time | Tick | Health | What |
|---|---|---|---|
| 0–302 s | 3996532–3999518 | 10/10 | all 8 `self` reads (0.3 s to 297.5 s) returned 10/10; no hostile hit in any tick (the planner's first State still showed `None/None`, see **Server behavior after the deploy**) |

### Retreat

Each Retreat got clear (the hostile left `hostile_range`, so `should_retreat` went false) within one queue. None reached a safe tile.

| Start | Goal | Ended at | Time |
|---|---|---|---|
| (370, 399) | safe (397, 401) | (380, 396), 17 cells short | 1.6–6.3 s |
| (401, 370) | safe (393, 397) | (393, 380), 17 cells short | 163–172 s |
| (391, 504) | safe (390, 408) | (381, 496), 88 cells short | 281–288 s |

### Hunting-ground search

**No hunting cell found.** The search ran the whole 300 s (`travel:hunting_ground searching: explore_area`, 36 decisions) and walked the frontier from town east to (427, 388), then south to (358, 519), 118 cells from town. It did not give up: `HUNT_SEARCH_SECONDS` is 300 too. All **211** zone reads went to the respawn ring around the town cell (x 390–404, y 394–408, all `safe`, no `strength_ceiling`). Not one was a hunt probe. Wartlurch 262, grubhulk 263 and gristlewick 264 stood around (367–400, 481–495), the first new monster types of the run, and no zone there was read. See defect 1.

### B133

- **Queues that never finish:** none. 44 queues sent. Every walk queue ended with `held_queue` cleared at the next poll. No lost-queue drop: a drop re-reads position, and the 5 position reads were the start, 2 after Flee reflexes dropped a queue (by design), and 2 after the two `Take`s.
- **Stale `finished_queue`:** none seen. The trace logs `held_queue` but not `finished_queue` (`runner.py:571`), so a stale one naming an older queue would not show. Queue ids changed on every send.
- Polls came late (defect 2), so a queue often finished 100–160 ticks before the poll that saw it. The character stood idle in that time.

### Server behavior after the deploy

No clear change. Two things to watch:
- Planner call 1 (0.7 s) saw `health=None/None`, although the `self` read at 0.3 s had already returned 10/10. The character was asleep at the start, and its health had not yet reached the State the planner reads. Later calls saw 10/10.
- In the profiling run, Flee sent `Step(left)` from (397, 617) toward walkable `tile` (396, 617) about 30 times over 35 s. Position never changed and no rejection came back. The trace does not log `intent_results`, so this run cannot tell a silent no-op from a rejection the agent ignored.

### Planner

| | |
|---|---|
| Calls | 19 (2 `applied`, 8 `unchanged`, 8 `kept`, call 19 still in flight at the end) |
| Plans accepted / errors | **10 / 0** |
| Tokens | input 10,785, output 2,547, cache write 0, cache read 1,778,292 |

Call 1 set `retreat_hits` 3 and `fight_margin` 2.0, and queued `travel to town`, `explore_area` around town, then (call 3) `gather_gems` 30. All of it stayed under the pinned `travel:hunting_ground` and never ran. Each later answer repeated "go to town first" while the character walked up to 92 cells away.

### Decision mix (44 decisions that sent intents; 88 more polls held a queue)

| Op / state | Decisions |
|---|---|
| travel (`hunting_ground` search: explore the whole map's frontier) | 36 |
| retreat | 3 |
| flee | 3 |
| take apple | 2 (both on supply 62581; see below) |

The two `Take`s went one poll apart (ticks 3998233 and 3998236). The first took the apple: the `SupplyTaken` for 62581 is stamped tick 3998233. It reached the agent only on the next response, so the second `Take` went to a supply that was already gone. Only one apple was taken. The trace does not log `intent_results`, so it cannot tell whether the second `Take` was rejected or a no-op. The two position reads after it suggest a rejection.

Intents: 325 `Step`, 849 `Wait`, 2 `Take`. Call mix: 211 `zone`, 132 `tick`, 64 `entities`, 37 `strategist`, 17 `terrain`, 8 `self`, 5 `position`, 1 `world`.

### Top 3 defects

1. **The hunting-ground search never read a zone outside town.** `next_zone_probe` puts every unread cell of the 17×17 respawn ring ahead of the hunt probes (priority 0 vs 1). Zone reads go only to spare windows, so the ring took all 211 reads and was still unfinished at 302 s. The hunt grid (`hunt_probes`) never got one, so `strength_ceiling` could not turn up. Run 1 did not show this because its knowledge base already held the town's zones. With an empty one, this happens to every new character. Also, `probe_until` lasts only 5 s after Travel last acted, while decisions came 5–20 s apart (defect 2), so the probes would mostly have been off anyway.

   ```
   t=3999331 @76:391,504 tick     queue 10×Step 27×Wait (retreat → safe (390, 408))
   t=3999342 @76:388,502 zone     @76:391,394 safe=True
   t=3999345 @76:388,502 zone     @76:391,408 safe=True
   t=3999518 @76:358,519 zone     @76:404,394 safe=True     (last read: still the ring, 125 cells away)
   ```

   Backlog: **A27**, reopened as `partial` in this PR.

   Suspects: `zone_discovery.py:102–110` (ring at priority 0 always beats hunt probes at 1; `RESPAWN_PROBE_RADIUS` 8 at `:19`), `brain.py:91–94` (zones only in spare windows), `states/travel.py:38` and `:110` (`HUNT_PROBE_FRESH_SECONDS` 5).

2. **The main thread blocks 5–17 s per decision, so the character stands idle and polls are late.** 17 gaps of 4–17 s with no call at all, 162 s of the 302 s run. Each gap ends in a `tick`, 15 of them held-queue polls. Reads take a median 0.3 s, so the time goes to computing the decision. The planner runs on its own thread and is not the cause: gaps also came with no call in flight (110–120 s). The profiling run shows where the time goes. The whole-map frontier explore of the hunting search took **13.7 s per call** (3 calls, 41.1 s). `reflex_while_held` took 53.5 s over 39 held polls, because it runs a full `_decide`. `navigation/planner.py` `danger()` ran 2.03 M times, with 30.9 M `chebyshev` calls under it. In a fight this would rule out urgent polling, because one decision outlasts a gristlewick's whole 16 s kill in Run 1.

   ```
   46.6 s  tick 3996982 queue held   (next_index 22 of 37)
   46.7 s  strategist ask; 47.1 entities; 47.6 zone
   63.3 s  tick 3997149 queue held   (queue long finished; 15.7 s and 167 ticks with no call)
   profile: explore_outcome 3 calls 41.1 s; reflex_while_held 39 calls 53.5 s; danger 2,030,476 calls
   ```

   Backlog: **A64** (new in this PR).

   Suspects: `navigation/planner.py:542–566` (`nearest_target` runs one unbudgeted A* per frontier target in distance order, and `continue`s past every unreachable one, so 358 whole-map targets can mean hundreds of full-box floods), `navigation/planner.py:97–107` (`danger` loops over every hostile for every expanded cell, and `hostile = ["npc"]` makes all townsfolk hostiles), `states/travel.py:40` (`HUNT_SEARCH_AREA` radius `EXPLORE_ANYWHERE`), `runner.py:603–619` (the held-queue probe repeats the full decision on every poll).

3. **Retreat and Flee fire on townsfolk.** 3 of the 6 survival episodes came from non-combat NPCs: Flee from rumor_teller 135 and apothecary 130 beside the town shops, and Retreat from salvager 160, 2 cells away, at 10/10. With `hostile = ["npc"]`, any NPC within `hostile_range` 2 forms a `combat_group`. `would_lose` then judges it unmeasured and outclassing at the planner's `fight_margin` 2.0, so a merchant turns the character around. Run 1 had the same with the farmer.

   ```
   t=3997503 @76:424,400 tick  Step (flee npc 135)      npc 135 = rumor_teller
   t=3997512 @76:423,399 tick  Step (flee npc 130)      npc 130 = apothecary
   t=3999331 @76:391,504 tick  queue 10×Step 27×Wait (retreat → safe (390, 408))   salvager 160 at (393, 503)
   ```

   Backlog: **A9**, reopened as `partial` in this PR.

   Suspects: `survival.py:50–58` and `:86–98` (`hostiles_in_range` and `combat_group` take every entity of a `policy.hostile` kind, with no way to tell a monster type from a townsperson), `survival.py:194–210` (`would_lose`), and `olympuff_m8.toml` `hostile = ["npc"]`.

**Minor:**
- The trace logs neither `finished_queue` nor `intent_results` (`runner.py:568–574`), so B133 queue ends and Step outcomes can only be inferred.
- The planner's stack sits under the pin all run, and its answers keep saying "go to town first" while the search walks away from town (up to 92 cells out) at 10/10 with a knife. Nothing caps how far the search goes from safety.
- Two position reads after a plain `Take` (172.3 s, 178.7 s).
