# A67 live free-play runs with the planner on (redacted)

Scenario: free play. `scripts/smoke_m8_olympuff.py --seconds 600` with the AI planner on, on a copy of `python/characters/olympuff_m8.toml` whose directives file holds no goals (`goals = []`), so the planner decides everything. Deaths do not end the run (no `--stop-on-death`), and the 60 s park phase runs after it. The character is chosen at run time, and no name or id is committed. It started with about 21 gems, so the progression arc (`strategist.py:176`) should take it to stage 2: buy and equip gear, then go on hunting gems.

## Free play

### Run 1: an accidental sword, then 370 s frozen in a Break loop that swaps weapons

- **Code:** `main` at `5ff4f0f` (after #133 A64 park and #134 A63 run 3 fixes).
- **Verdict:** exit 0, `PASS` after **600.9 s**, on the **short-run gates only**. At 600 s the M8 full-run clauses are skipped (`full_run` needs at least 95% of 3,600 s, `smoke_m8_olympuff.py:116`). So the run passed while it stood frozen for 370 s, earned no gems and wore no armor. The park timed out after 60.3 s, still at (362, 377), and cleared the queue. The character started at 7/10 health, 7 lives and 21 gems, armed with the pocket knife, on the overworld 17 blocks north-east of the town cell.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **no**; armor **no**; shop weapon **yes** (by accident, below); potion reserve, heal takes all **no**.

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–44 s | `travel` town, `say` farmer → `say` chugbug, `explore_area` town r20 | Stage 1 ("survive and learn"). Call 1 ran before the first `self` read and saw `gems=None health=None/None`. From call 2 it saw 21 gems but kept to stage 1 |
| 44–141 s | `travel` town, `say` storekeeper / smith, `buy` small_potion ×1–2, `explore_area`, briefly `break_block` (379, 375) | **Went shopping**: potions first. Shop walked toward (422, 398) seven times and never arrived. Oscillation gave up an explore target at 123 s |
| 141–155 s | `travel` **shop**, `buy` small_potion ×2, `explore_area` | `travel to: shop` (x, y 0, 0) went to the nearest shop cell, (414, 398). It walked onto the **bronze_sword** lying there and took it: **gems 21 → 6** (defect 2) |
| 155–221 s | `equip` bronze_sword, `gather_gems:20`, `buy` small_potion ×2, `travel` town | Armed the sword at 159 s. Cut 3 grass and 3 bushes south and west of town, **0 gems** |
| 221–600 s | `travel` point (370, 370) (`nearest_safe`), `equip` bronze_sword, `gather_gems`, `travel` town, `buy` small_potion | Stood at (362, 377) from **231 s to the end**. Break flipped Arm between knife and sword 288 times (defect 1). From 502 s the stack was down to `travel` (370, 370), then `gather_gems:30` |

Planner calls: **44** (20 `applied`, 24 `unchanged`), 44 plans accepted, 0 errors. 22 of the 24 `unchanged` replies came in the frozen stretch. Each claimed to have "dropped the placeholder 0,0 from the town travel" and re-sent the same stack (defect 3).

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **21** | **6** (−15, the bronze sword; 0 earned) |
| Armed | pocket_knife | flipping every ~1.5 s; at the last read, bronze_sword |
| Worn | `{}` | `{}` |
| Held | `{}` | `{"pocket_knife": 1}` (the weapon not armed) |
| Potions | 0 | 0 (6 gems cannot pay for one) |

Bought: one bronze_sword, by stepping onto it on a `travel:shop` walk, not by any `buy` op (the only `buy` ops named small_potion). Equipped: the sword, by the planner's `equip` op at 159 s. Armor: none.

#### Gems earned

**0.** 6 cuts took effect (3 grass, 3 bush), 0 `applied_no_effect`, no gem drops; `gather_run={"cuts": 6, "gems_gained": 0}`. The 370 s freeze cost the rest of the hunt.

#### Deaths

**0** (lives 7 → 7). Health stayed 7/10 all run: no `Damaged` events, and Heal reported "no safe-zone regen this run".

#### Greetings and clues

Greet said hello to **21** NPCs, among them snotlings and gristlewicks the run never saw attack. 10 helpers answered, and all 10 landed in Clues:

| Speaker | Clue |
|---|---|
| statue_carver | "I carved the eight in town. I carved one facing the wrong way. On purpose." |
| elder | "Eight gods keep eight temples … Three of them guard a piece of their father's bolt. Put the pieces together, and the last door opens." |
| tongues_lady | ".ti dniheb kool .yawa secaf eno .seutats eht tnuoc" (reversed: "count the statues. one faces away. look behind it.") |
| pond_fisher | the brother on the big east lake sells rafts; sandals "made somewhere hot" walk on water |
| rumor_teller (5) | NW potato patch, take matches; moose-hills thicket where archers live; south steaming ground, door wants "a key that sprouts"; a door on an island in the east lake; a west hedge maze, "buy eyes at its door" |
| farmer | "That rock in my garden rings hollow when you knock it." |

The planner read them well: it decoded the reversed line, tied the hollow rock to (379, 375) and a 40-gem bomb, and parked both for later. No `say` op ever reached its NPC (`npcs_spoken_to=0`): each was dropped when the NPC left `nearby_npcs`, or because Greet already had its line.

#### Level entrances

**8 known, none visited or entered.** The world knowledge base holds 8 entrance cells on map 76, from (400, 37) to (763, 763). The planner's State lists none of them (`strategist.py:509–520`). It cannot tell when stage 1's "at least one level entrance is known" is met, and it never named an `entrance`.

#### Tokens

| | |
|---|---|
| Tokens | input 76,469, output 11,411, cache write **0**, cache read 4,396,436 |
| Tokens per minute (budgeted) | **~7.4–13.0k/min** (minute 2 the highest), ~7.6k in the frozen stretch |

The cached prefix was still warm from an earlier session, so no call wrote the cache and minute 1 had no ~100k spike (A63 runs 1–3 had one).

#### Decision mix (343 decisions that sent intents)

| Decision | Count |
|---|---|
| `break → …` (stuck step 2): a lone `Arm` | **288** |
| greet | 21 |
| explore, travel:town, travel:shop | 20 |
| heal_explore, shop small_potion | 14 |
| gather walk, cut grass, cut bush | 13 |

Intents: 373 `Step`, 1,055 `Wait`, **289 `Arm`**, 21 `Say`, 6 `Use`, 0 `Take`. Call mix: 896 `tick`, 95 `entities`, 88 `strategist`, 67 `self`, 58 `zone`, 17 `terrain`.

#### Top 3 defects

1. **Break flips Arm between two weapons forever and never steps** (A68). At 231 s the walk to the planner's (370, 370) reached stuck step 2. From then on every Break decision sent one `Arm`: knife (2350), then sword (20272), then knife again, 288 times over 370 s, at the same cell. It never stepped, never used a block, and never escalated. The planner, Park (`safe tile unreachable`) and the M8 gates all missed it.

   ```
   t=4045761 @76:362,377 tick  Arm (break → (365, 380))    supply 2350
   t=4045778 @76:362,377 tick  Arm (break → (365, 380))    supply 20272
   t=4045791 @76:362,377 tick  Arm (break → (370, 370))    supply 2350
   t=4045813 @76:362,377 tick  Arm (break → (370, 370))    supply 20272
   …  (288 in all; State flips armed=pocket_knife / bronze_sword between planner calls)
   t=4050044 @76:362,377 park  park timed out at 76:362,377 after 60.3s
   ```

   Suspects: `break_memory.py:157` picks a tool from `w.held_supplies` only, and that list leaves out the armed weapon. When two held weapons share the capability, the weapon not armed always wins, so each Arm makes the other weapon the choice. `held_supplies(w)` at `:112` already adds the armed one back. `states/break_state.py:89–123` returns `[Arm, SetPosition]` unpaced, and `brain.py:142` sends only the first intent, so the step never goes out. The stuck clock (`break_state.py:116`) escalates only when there is no step, and here there was one every time.

2. **`travel to: shop` walks onto a shop item and buys it** (A69). With no x, y, Travel resolves `shop` to the nearest shop cell (`travel/resolve.py:68`, cells from `travel/knowledge.py:129`), and that cell is the item for sale. The walk ended on (414, 398) and took the bronze_sword, 15 of 21 gems, while the planner's only `buy` ops named small_potion. That left 6 gems: no potions and no armor, and stage 2's readiness was out of reach for the rest of the run.

   ```
   call 13  travel to=shop x=0 y=0, buy small_potion, buy small_potion   "Buy potions first"
   t=4044916 @76:414,398 tick  — (queue held) | SupplyTaken 20272   gems 21 → 6
   call 14  "Can't afford potions at 6 gems; gather first, then buy"
   ```

   Travel should stop next to a shop cell, never on one; buying is Shop's job. Earlier, Shop's own walk to (422, 398) never arrived: it started seven times between 69 s and 132 s, four of them from (406, 392), where oscillation gave up an explore target.

3. **The planner cannot see a stall, and churns on the 0, 0 placeholder instead** (A70). The State has no line saying the character has not moved, or that the top op's executor is stuck in a loop. For 370 s the planner saw `pos=76:362,377` with a `travel` (370, 370) on top and kept the stack ("Keep the retreat on top"). Its replies instead fixed something that was not broken. `plan.py:157` turns a symbolic `travel` with no x, y into 0, 0, and `strategist.py:656` prints that back in the stack. 22 replies said they had "dropped the placeholder 0,0" and re-sent the same op, which the plan filled with 0, 0 again. At 234 s the same doubt ("The travel target of town at x=0,y=0 looked odd") is why it swapped `travel` town for `travel` (370, 370), the walk that led into defect 1.

   ```
   call 20  "The travel target of town at x=0,y=0 looked odd, so I replaced it with a nearby safe tile."
   call 27  "Removed the placeholder 0,0 from the town travel."
   call 36  "Dropped the invalid 0,0 coordinates on the town travel so it uses the known town."
   call 44  "Keeping the retreat on top."   pos=76:362,377 since 231 s
   ```

   Suspects: `strategist.py:509–525` (no time-at-cell or top-op progress line, no entrances) and `strategist.py:656` (shows the 0, 0 the planner never sent). Omitting x, y for a symbolic `to` when they are 0, 0, and adding something like `stalled_at_cell_s` with the top op's last decision, would let the planner see this stall.

**Minor:** call 1 ran before the first `self` and inventory reads (`gems=None`, `health=None/None`, `armed=None`), so the run's first plan was made blind. `nearest_safe` pointed at (370, 370), but every zone read near it came back `safe=False`. Greet said hello to 6 snotlings and gristlewicks, monster-looking types that had not yet hit anyone this run, so they counted as not hostile.

### Run 3: a potion stuck in the weapon slot, so 33 equip ops and every cut did nothing

- **Code:** `main` at `bd5aeff`, after #135 (one hostility test), #137 (Heal walk, Take resend, gather/retreat pacing), #139 (run 1 fixes: Break arm flip-flop, shop tiles, planner stall view and entrances) and #140 (run 2 fixes: hub give-ups lapse, reachable safe tile, gather target region).
- **Verdict:** exit 0, `PASS` after **601.5 s**, on the short-run gates only (as in run 1). The park timed out after 60.3 s at (369, 369) with `safe tile unreachable` (defect 3). The character started with 10/10 health, 7 lives and **17 gems**. It had the bronze_sword armed and the pocket knife held, and stood on the overworld 34 blocks south-west of the town cell.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **yes** (from piles only); armor **no**; shop weapon **yes** (the sword it started with); potion reserve **no**; Heal took food on the ground **no**, Heal drank a potion **no**.

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–66 s | `travel` town, `explore_area` town r20, `gather_gems` | Fled chugbugs at the start, then Heal walked to safe ground and ate an apple. Reached the town cell at 66 s. **No give-up on town or the shop all run.** |
| 66–123 s | `read` (399, 406), `explore_area` town, `buy` small_potion, `gather_gems`, `buy` bronze_mail | Greet worked through the town NPCs. The planner tried to read the statues for the reversed clue, and the reads looped. |
| 123–184 s | `travel` town, `buy` small_potion, `gather_gems`, `buy` bronze_mail, `equip` | Explore went 60 cells south, where it was hit for 3. Retreat brought it back to safe ground. |
| 184–195 s | `buy` small_potion | Shop walked to (421, 399) and took the potion with `Take` (**gems 17 → 7**). Heal then sent `Arm` small_potion twice and never sent the `Use`. From 191 s the potion is armed (defect 1). |
| 195–600 s | `equip` bronze_sword (33 stacks), `gather_gems`, `buy` bronze_mail | Every `equip` finished "nothing left to equip" (145 decisions), and the potion stayed armed. Gather cut grass and bushes with the potion, and no cut had an effect (defect 2). Gems came back to 17 from gem piles only. |

Planner calls: **56** (50 `applied`, 5 `unchanged`, 1 kept). 55 plans accepted, 0 errors. From call 19 on, nearly every reply reported that "the potion is still armed" and re-sent `equip`, by code or bare, sometimes both.

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **17** | **17** (−10 for the potion, +10 from piles) |
| Armed | bronze_sword | **small_potion**, from 191 s |
| Worn | `{}` | `{}` |
| Held | `{"pocket_knife": 1}` | `{"pocket_knife": 1, "bronze_sword": 1}` |
| Potions | 0 | 1, armed and never drunk |

Bought: one small_potion, by the planner's `buy` op. Equipped: nothing. The potion was armed by Heal, not by an `equip` op. Armor: none, since bronze_mail costs 20.

#### Gems earned

**10**, all from gem piles: 5 `Take` on gems, plus pickups on walks. Gather sent 39 cuts (grass and bush). The last `gather_run` the planner saw was `{"cuts": 0, "gems_gained": 6, "no_effect_cuts": 35}`, and its `gather_status` alternated between "cuts have no effect here" and "walking to a gem pile". The planner never named a `gather_gems` region.

#### Deaths

**0** (lives 7 → 7). Hits: 1 from chugbugs at the start, and 3 in the south field at 128 s. Health was 10/10 at the end.

#### Greetings and clues

Greet said hello to 17 NPCs, with the same lines as run 1: statue_carver, elder, tongues_lady (reversed), pond_fisher, five rumor_tellers and the farmer. The planner decoded the reversed line again and tied it to the carver's statue. It sent `read` ops at the statues, which looped, and dropped them at 123 s.

#### Level entrances

**8 known, all now in the planner's State** (#139), each with a distance: the nearest is (300, 316), 67 cells north-west. **None visited or entered**, and no op named an `entrance`. The run never got past stage 2's gear step.

#### Tokens

| | |
|---|---|
| Tokens | input 116,316, output 18,705, cache write 100,503, cache read 5,527,665 |

Call 1 wrote the cache (~100k), as in A63 runs 1–3.

#### Decision mix

Intents outside held queues: 39 `Use` (cuts), 17 greetings, 6 `Take` (5 gems, 1 potion), 3 `Read`, **3 `Arm`** (two potion, one sword re-arm), plus queued walks. Call mix: 707 `tick`, 236 `entities`, 217 `zone`, 112 `strategist`, 60 `self`, 41 `terrain`.

#### Run 2 fixes, checked

| Run 2 defect | Run 3 |
|---|---|
| A town give-up lasts the whole run (#140 §1) | **Not exercised.** Travel gave up nothing. It reached the town cell at 66 s, and Shop reached the potion at 190 s. |
| A safe tile that cannot be reached (#140 §2) | **Not fixed for Park.** At (369, 369), 2 cells from the chosen safe tile (371, 371), Park sent nothing for 60 s with `safe tile unreachable`. Retreat at 350 s did reach its safe tile. |
| Gather steering (#140 §3) | **Inconclusive.** No `gather_gems` x, y was named. Gather moved through 9 regions, but every cut had no effect because of the armed potion (defect 2), so its yield data is noise. |

#### Top 3 defects

1. **Heal's drink sends the `Arm` and never the `Use`, and the stale re-arm blocks Equip for the rest of the run.** At 191 s, on safe ground at 7/10 health, Heal returned `[Arm potion, Use self]` (`states/heal.py:270`). The brain keeps only the first intent (`brain.py:144`), so only the `Arm` went out, twice. `m.heal_rearm` stayed `bronze_sword`, and Equip treats the armed slot as Heal's while it is set (`states/equip.py:50`). So `best_equip_upgrade` sees no upgrade, and every `equip` op finishes at once (`states/equip.py:31–32`): 33 planner stacks, 145 "nothing left to equip". The potion was never drunk.

   ```
   190 s  Take(1844) (buy small_potion)       gems 17 → 7
   191 s  Arm (arm and use small_potion)
   193 s  Arm (re-arm bronze_sword)
   194 s  Arm (arm and use small_potion)      armed = small_potion until the end
   call 23  "The previous equip finished with nothing left to equip, yet small_potion is still armed."
   call 56  "If it finishes with the potion still armed, the equip op itself is broken."
   ```

2. **Gather cuts with whatever is armed, then trusts the result.** `states/gather.py:353` sends `Use` on grass or a bush without checking that the armed item cuts. With the potion armed, all 39 cuts did nothing. Each no-effect cut feeds `GemYieldTracker`, and `gem_cuts.uncuttable` (`states/gather.py:225`) then writes off those cells and regions. That poisons #140's region steering, and the planner's `gather_status` says "cuts have no effect here" for ground that cuts fine with a blade. Gather should arm a cutter, or report "no cutter armed", before cutting.

3. **Park and Retreat keep picking a safe tile they cannot walk to.** `pathing.reachable_safe_goal` counts a search cut short by its budget as a way (`pathing.py:407`), so (371, 371) passed. Retreat then found no step to it, but marks a tile unreachable only when `no_way` proves it (`states/retreat.py:131`). So the same tile was picked on every decision, and Park stood 2 cells from it for 60 s. A failed `cost_path` to a tile `reachable_safe_goal` just accepted should mark it in `safe_unreachable` too, or fall through to town.

**Minor:** the planner sent `read` at a town statue cell three times, and each read looped until it was dropped. Explore took the character 60 cells south of town into a hostile field while it had no potion.

### Run 4: committed targets and detours work, but the detour misses gems beside the queued walk

- **Code:** `main` at `a227906`, after #154 (A71: commit to the target, the Detour reflex, discovery wakes, new heads deferred to an action boundary) and the run 3 fixes (#146, #147, #149, #151, #152).
- **Verdict:** exit 0, `PASS` after **601.5 s**, on the short-run gates only. The park found safe ground at once (0.4 s, at (421, 381)). The character started with 10/10 health, 7 lives and **17 gems**, on the overworld 32 cells north-west of the town cell. It held the bronze_sword and the pocket knife, and **small_potion was still armed** from run 3. This was the first run on an empty local state directory, so call 1 wrote the cache.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **yes** (from piles only); armor **no**; shop weapon **yes** (the sword it started with); potion reserve **yes**; Heal took ground food **yes**, Heal drank a potion **no**: the gate read yes, but it counts a `Use` sent with a potion armed (`m8_acceptance.py:140–142`), and that `Use` at 228 s consumed nothing (potions 2 → 2, below).

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–60 s | `travel` town, `explore_area` town r25; from 16 s `equip` bronze_sword on top | The first decision, before any plan, sent a 10-step Explore walk north-west. It passed 2–4 cells from three adjacent gems at (359–361, 360), and Detour did not take them (defect 1). Travel then walked to the town cell and Greet said hello on the way. |
| 60–147 s | `equip` bronze_sword, `buy` small_potion, `say` statue_carver, `explore_area` (410, 400) r15 | Each `equip` finished at once (defect 2). With no priced supply in sight, Shop sent nothing, so the Explore safe default walked **~100 cells south-west**, to (299, 390) (defect 3). |
| 147–210 s | `travel` town, `buy` small_potion, `say`, `explore_area` | Break armed the sword at 147 s. A chugbug pack hit it 10 → 6; Flee and Retreat got clear, and Heal walked back through safe ground and ate an apple. |
| 210–270 s | `say` rumor tellers, `buy` small_potion, `gather_gems`, then `equip` bronze_sword ×3 | Heal **re-armed the potion** at 213 s. Shop bought a potion at 226 s (**gems 17 → 7**). Gather re-armed the sword before cutting at 267 s (#152). |
| 270–600 s | `gather_gems` (4 regions named in 50 s), `travel` town / point, `buy` bronze_mail, `buy` small_potion | 64 cuts, none with no effect, and no gem from a cut. The gems came from 3 piles. Travel paced twice, and a snotling pack was retreated from at 485 s and 556 s. |

Planner calls: **53**, 53 plans accepted, 0 errors. 19 replies with a new head were **deferred** to an action boundary, none for long.

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **17** | **14** (−10 for one small_potion, +7 from gem piles) |
| Armed | small_potion (left from run 3) | bronze_sword (from 267 s) |
| Worn | `{}` | `{}` |
| Held | bronze_sword, pocket_knife | pocket_knife, small_potion ×2 |
| Potions | 1 (armed) | 2 |

Bought: one small_potion, by the planner's `buy` op. Equipped: the sword three times, by Break (147 s) and Gather (267 s) but never by an `equip` op. Drank: Heal sent `Use` on the potion at 228 s at 8/10, but the count went from 2 potions to 2, so nothing was drunk. Armor: none, since bronze_mail costs 20.

#### Gems earned

**7**, all from three gem piles: (413, 414) +3, (361, 440) +3 and (446, 376) +1. Detour walked to each of them. `gather_run={"cuts": 64, "gems_gained": 7, "no_effect_cuts": 0}`, so #152's cutter fix holds: no cut was wasted, though none dropped a gem. Each pile was a triple of adjacent gems. The (359–361, 440) triple was passed at 96–98 s and taken only 250 s later (defect 1). Two triples were never taken: (359–361, 360), passed at 2–6 s (defect 1), and (377–379, 377), which no walk came within Detour's 3 cells of.

#### Deaths

**0** (lives 7 → 7). Hits: chugbugs at ~150 s (10 → 6), and snotlings at 485 s (→ 7) and ~560 s (→ 5). Flee, Retreat and Heal handled each one.

#### Commitment, detours and planner wakes (#154)

- **Targets held.** Gather kept its target until it was cut or taken: the (446, 376) pile was kept from 526 s until it was taken at 539 s. Explore's targets moved on steadily, never back and forth.
- **Flip-flops left.** (a) 15 `equip` ops (calls 3–15 and 23–25) each finished "nothing to equip" at once, and the planner re-sent them (defect 2). (b) Travel to town paced (370, 430) ↔ (369, 429) five times, and the oscillation guard gave town up at 391 s. It paced again at (430, 370) ↔ (429, 369) at 550 s. The leg target flips each decision to the cell it is not on (`states/travel.py:242–245`, `_map_leg`/`nav_stuck.leg_toward`). (c) The planner named four `gather_gems` regions in 50 s (calls 41–46).
- **Detours: 5.** Gems at (413, 414), (361, 440), (446, 376) and (428, 366) (cut off by the end of the run), and an apple at (391, 385) while at 5/10. Each then went back to the walk it left. All four gem detours aimed at piles 10–16 cells off, which Gather or the walk was already heading for.
- **Wakes: 18 of 53 calls were discovery wakes** (new NPCs, hostile packs, affordable small_potion and torch). 6 reordered the stack: calls 3–4 put `equip` first and raised `retreat_hits` for the gristlewick and chugbugs, call 19 put talking to the rumor tellers first, and calls 34, 44 and 48–49 retreated from packs. The rest were rightly left unchanged (packs 20+ cells away, affordable items it already planned to buy).

#### Tokens

| | |
|---|---|
| Tokens | input 144,469, output 17,457, cache write 100,743, cache read 7,454,982 |

#### Decision mix

Decisions outside held queues: 61 cuts (44 bush, 17 grass), 42 Gather walks, 25 Travel, 21 Explore, 22 greetings and 2 says, 9 Detour, 11 Heal walks, 7 Retreat, 2 Flee, 5 `take gem`, 4 Break. Intents: 752 `Step`, 2,185 `Wait`, 67 `Use`, 24 `Say`, 10 `Take`, 3 `Arm`. Call mix: 660 `tick`, 210 `entities`, 201 `zone`, 128 `strategist`, 54 `self`, 47 `terrain`, 15 `position`.

#### Top 3 defects

1. **Detour misses gem piles a few cells off the walk, so they are taken on a later pass or never** (A73). Two triples show it.

   **(359–361, 440), taken 255 s after it was first seen.** First seen at 87–89 s. At 96–98 s the Explore safe default (defect 3) walked past it, 5 cells off, from (376, 446) to (361, 433). Nothing went for it. The nearest path cell was past `DETOUR_REACH` = 3 (`states/detour.py:40` at 4e31dc0), and going by it added about 7 steps, over `DETOUR_EXTRA_STEPS` = 4 (`states/detour.py:42`). A73 removed both. The walk under way was Explore's, not Gather's, so no other state took gem piles. It went back at 331 s only because Gather, working a `gather_gems` op from 199 s, picked the pile as its target ("gather → (361, 440)"). Detour took the last stretch at 336 s, and the three gems were taken at 342–349 s. The planner never named the pile. To take a triple on the first pass, Detour should judge a gem pile in view by the steps it adds, with a larger allowance for a pile, not by a fixed 3-cell reach.

   ```
   87 s   @383,448  (359..361, 440) first in view
   97 s   @366,437  explore → (361, 433)               5 cells off, no detour
   331 s  @385,434  gather → (361, 440)                 Gather picks the pile
   336 s  @375,433  detour → gem at (361, 440), then back to gather
   342–349 s  take gem ×3                               gems 10 → 13
   ```

   **(359–361, 360), never taken: the queued walk is cut off the path Detour prices against.** (Fixed in #158: Detour now checks the held queue's remaining Steps, then `m.path`, `pathing.route_ahead`.) When a walk queue goes out, `runner.py:944` drops the queued cells from `m.path` (`m.path = m.path[queued:]`). While that queue runs, the held-queue probe runs Detour, and `detour_find` (`states/detour.py:114`, `:121`) measures each find from where the character stands against a path that starts up to 10 steps ahead. Cells beside the queued stretch are not on it at all, and later cells are counted as nearer than they are. At 2–6 s the first Explore walk, (369, 369) toward (344, 356), queued 10 steps through (363, 365) and (359, 364), 2–4 cells from three adjacent gems at (359–361, 360). From (363, 365), `extra_steps` on the cut path gives 5, 6 and 7 extra steps, over `DETOUR_EXTRA_STEPS` = 4. On the rest of the walk it gives 1, 2 and 3. The gems were in view, and no state suppressed Detour; it never fired. When the queue ended at 6 s, Travel took over toward town, away from them. Detour should price against the held queue's remaining cells plus `m.path`.

   ```
   t=2 s  @369,369  queue 10×Step 27×Wait (explore → (344, 356))     m.path = cells 11+
   t=4 s  @363,365  queue held   gems (359..361, 360) in view   extra_steps 5/6/7 (cut) vs 1/2/3 (rest of the walk)
   t=6 s  @359,364  travel:town → (369, 385)                        gems 4 cells north, left behind
   ```

2. **Equip never takes a non-weapon out of the armed slot** (A74). `best_equip_upgrade` considers a weapon only while the armed slot holds a weapon or nothing (`equip.py:175`: `not armed_owned and (armed is None or is_weapon(armed))`). A potion that no state owns, here left armed from run 3, blocks the sword for good. Every `equip` op then finishes at once (`states/equip.py:31–32`): 15 ops over 250 s, each re-sent by the planner ("the armed slot still holds a small_potion"). The sword was armed only incidentally, by Break's cut at 147 s and Gather's re-arm at 267 s. Heal makes it worse: `use_carried_heal` records whatever is armed as the weapon to restore (`states/heal.py:308–309`), a potion included, and at 213 s it **re-armed small_potion** over the sword. Equip should treat an unowned armed non-weapon as an empty slot, and Heal should record only a weapon.

3. **A `buy` with no shop item in sight sends nothing, and the Explore safe default walks away from town** (A75). `shop_outcome` returns "nothing to buy" when no priced supply is in view (`states/shop.py:66–67`), and nothing inserts a `travel` shop stop first, the prerequisite rule #154 added for entrances. From 60 s to 133 s, with `buy` small_potion on top, Explore walked from the town cell to (299, 390), about 100 cells south-west, into a chugbug pack that hit it 10 → 6. The shop was 25 cells east of the town cell. Shop should insert a `travel` to the nearest known shop cell as a stop, or Travel should resolve one, before falling through.

   ```
   64 s   applied  equip, buy small_potion, explore_area (410, 400) r15
   65–133 s  explore → (395, 436) … (291, 385)    17 gems, no shop item in view
   152 s  flee chugbug ×2, health 10 → 6
   223 s  shop small_potion → (422, 398)          first sight of the shop
   ```

**Minor:** Travel paced between two cells twice (above, `states/travel.py:242–245`). #158 fixed it: the window search now ranks cells by the corridor tree. The Use at 228 s on a just-bought potion at 8/10 consumed nothing, yet the M8 potion gate counted it (A76). The planner retargeted `gather_gems` four times in 50 s on 4–17-cut samples, though Gather already relocates itself after 20 cuts.
