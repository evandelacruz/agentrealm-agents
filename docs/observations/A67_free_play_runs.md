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

### Run 5: run 4's fixes hold, but the probe uses up Heal's tries on both potions, and a gristlewick kills it with both still held

- **Code:** `main` at `40e82f1`, after #158 (A71, A13, A9: detour priced from the route ahead, the travel window ranked by the corridor tree, the gather region kept, the Retreat window) and #157 (A19, A21, A24, A74, A75: a non-weapon in the armed slot replaced, Heal's re-arm always a weapon, the drink targets `self`, a `buy` walks to the shop).
- **Verdict:** exit 0, `PASS` after **601.9 s**, on the short-run gates only. The park **timed out** after 60.3 s at (430, 378), with "Heal: no safe-zone regen this run; Park: safe tile unreachable" on every decision, and cleared the queue. The character started with 7/10 health, 7 lives and **14 gems**, on the overworld 24 cells north-east of the town cell. It held the pocket knife and **two small_potions** left from run 4, with the bronze_sword armed. The local state directory was empty again.
- **Gate summary:** deaths **1**; API errors **0**; fights below the health floor 0; gems earned **yes**; armor **no**; shop weapon **yes** (the sword it started with, lost at the death); potion reserve **yes**; Heal took ground food **yes**, Heal drank a potion **yes** (the one bought after the death; the drink went through). Planner: 52 calls, 50 plans accepted, 2 errors (prose replies, "payload is not an object").

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–97 s | `travel` town, `say` elder and rumor tellers, `buy` small_potion, `explore_area` town r25 | Heal explored the safe zone at 7/10 for 25 s instead of drinking (defect 1). Travel walked to town, and Greet said hello to 19 NPCs on the way and in town; 10 gave clues. |
| 97–168 s | `explore_area`, `gather_gems` 25, `buy` matches (for the "potato patch, take matches" clue) | Detour took the (413–415, 414) pile (+3). Shop walked to the matches in sight and bought them (**17 → 12**). |
| 168–300 s | `gather_gems` 60 (region (416, 384) from 238 s), `buy` small_potion ×6, `travel` town | 13 cuts, 2 gems. Gather and Detour went for the piles at (439–441, 360) and (428, 366) four times, and each time the gristlewick near (430, 370) hit it (10 → 3) (defect 2). Each `buy` small_potion finished at once, since 2 were held (defect 3). |
| 300–346 s | `travel` town / point, `buy` small_potion, `gather_gems` | At 3/10, Travel to town escalated to Break at (429, 385), and the town walk was given up. The Explore safe default then walked north, and Detour went for a gem at (429, 351) past the gristlewick (→ 2/10). Break armed matches beside it, Explore walked north again, and it was **killed at 346 s** at (436, 369), still holding both potions. |
| 352–415 s | `buy` small_potion, `gather_gems` 60 at (416, 384) | Respawned in town with the pocket knife. Shop walked to the potion in sight and bought it (**14 → 4**). Detour took the (439–441, 360) pile (+3). A snotling hit it at 9/10, and Heal drank the new potion (9 → 10) and re-armed the knife. |
| 415–600 s | `gather_gems`, `travel` point (429, 370) / town, `wait` 15–30 s | The gristlewick hit it 10 → 4 again around (430, 370). Travel to the point and to town were each given up by stuck detection. 35 decisions sat on planner `wait` ops at 5/10. |

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **14** | **7** (+8 earned, −5 for matches, −10 for one small_potion) |
| Armed | bronze_sword | pocket_knife (the sword was lost at the death) |
| Worn | `{}` | `{}` |
| Held | pocket_knife, small_potion ×2 | none |
| Potions | 2 | 0 (both lost at the death; the one bought after it was drunk) |

Bought: matches (5) and one small_potion (10), each by a `buy` op, each walking straight to the item in sight. Equipped: no `equip` op was needed or sent. Break armed the matches at 313 s and 339 s to burn a block, and Retreat re-armed the sword in between. Drank: one small_potion at 414 s; `SupplyUsed` came back and health went 9 → 10. The death at 346 s dropped the sword, the matches and both starting potions into a chest at (436, 369).

#### Gems earned

**8**: the (413–415, 414) pile +3 at 137–141 s and the (439–441, 360) pile +3 at 405–409 s, both by Detour, and 2 from cuts ((431, 391) at 208 s and (430, 392) at 273 s; 23 cuts in all, none with no effect). The pile at (428, 366) and the gem at (429, 351) sat by the gristlewick's post and were never taken. Piles at (377–379, 377), (359–361, 440) and (439–441, 440) never came within 10 cells of a walk.

#### Deaths

**1** (lives 7 → 6), at 346 s, killed by the gristlewick (npc 217) at (436, 369). It hit 12 times for 16 damage over the run, all within about 12 cells of (430, 370). A snotling hit once. The death came at the end of four trips back to its post (below), at 3/10 and then 2/10, holding two potions it could no longer drink (defect 1).

#### Run 4 fixes, checked

| Run 4 defect | Run 5 | |
|---|---|---|
| Detour pricing (A73, #158) | **Fixed.** No gem pile was passed within Detour's reach and left. 7 gem detours started. They took 2 piles (6 gems), and 4 were broken off by the gristlewick, not by pricing. | Run 4: 2 triples missed. |
| Travel pacing (#158) | **Fixed.** 0 Travel oscillations. The guard fired once, on Break ↔ Flee at (430, 371) ↔ (430, 372) at 482 s. | Run 4: 2. |
| Gather region churn (#158) | **Fixed.** The planner named 1 region, (416, 384), and kept it or left x, y out. Gather's own target still left the region for gem piles (defect 2). | Run 4: 4 regions in 50 s. |
| Potion in slot, equip no-ops (A74, #157) | **Fixed.** 0 `equip` no-ops, and no potion stayed armed: after the drink the slot was empty for 5 s, then Heal re-armed the knife (1 of 1). | Run 4: 15 no-ops, a potion re-armed. |
| The drink (A24, #157) | **Fixed.** 1 drink sent, 1 went through (`SupplyUsed`, 9 → 10). | Run 4: 0 of 1. |
| Shop drift (A75, #157) | **Fixed, untested out of sight.** Both buys walked straight to an item in sight; 0 drift. No `buy` came up with the shop out of sight, so the walk to an unseen shop did not run. | Run 4: ~100 cells off. |

#### Tokens

| | |
|---|---|
| Tokens | input 147,773, output 17,327, cache write 100,917, cache read 7,064,190 |

#### Decision mix

Decisions outside held queues: 54 no state (the park), 35 planner waits, 32 Detour, 28 Gather walks and 23 cuts (13 bush, 10 grass), 19 Explore, 19 Retreat, 18 Flee, 14 Travel, 21 Break, 19 greetings, 13 respawn syncs, 5 Heal walks, 1 drink and 1 re-arm, 5 `take gem`, 4 shop walks and 2 buys. Intents: 923 `Step`, 2,510 `Wait`, 32 `Use`, 19 `Say`, 10 `Take`, 5 `Arm`. Call mix: 796 `tick`, 257 `zone`, 238 `entities`, 123 `strategist`, 60 `self`, 37 `terrain`, 34 `position`.

#### Top 3 defects

1. **The held-queue probe uses up Heal's tries without sending the drink, so both starting potions were written off in the first 6 s.** `reflex_while_held` (`runner.py:584`) runs the reflexes, Heal among them (`states/dispatch.py:113`), on every "queue held" poll. Heal's `use_carried_heal` counts a try (`note_try`, `states/heal.py:308`) before anything is sent. Heal's drink is not marked a reflex (`_out`, `heal.py:174`), so the runner drops it (`runner.py:775`) and the walk queue runs on. The probe saves and restores the plan, path, targets and `heal_rearm` (`runner.py:760–783`), but not `m.heal_tries`. After the first walk at 1.5 s, 6 polls (2 potions × `HEAL_MAX_TRIES` = 3, `healing.py:43`) left neither potion usable (`tries_left`, `healing.py:55`; `carried_heal`, `:73`). From 6 s Heal explored the safe zone at 7/10 instead of drinking. At 288–346 s Heal could not run with the gristlewick in range (`_wants_heal`, `heal.py:90–92`), and Retreat's losing-ground drink (`_turn_on_losing`, `retreat.py:216–218`) found nothing usable either. It died at 2/10 holding both. The potion bought after the death had a fresh id and was drunk in a decision window. The probe should save and restore `heal_tries`, or count a try only once the intents are sent.

   ```
   1.5 s   @421,380  explore → (417, 405)          10×Step 27×Wait
   2.1–5.8 s  queue held ×6                          probe: Heal picks a potion, note_try, dropped
   6.4 s   @414,390  heal in safe ground: heal_explore   7/10, small_potion ×2 held
   301 s   health 3/10, 338 s 2/10                   gristlewick in range: Heal off, Retreat finds no usable potion
   346 s   Died (killed) at (436, 369)               dropped: bronze_sword, matches, small_potion ×2
   414 s   arm and use small_potion (new id)         SupplyUsed, 9 → 10
   ```

2. **Gather and Detour send it to gem piles beside a known hostile's post** (A22, A73). `gather_ground` (`states/gather_safe.py:62`) bars a cell only for a hostile in view within its bar of that cell. The gristlewick roams near (430, 370) (`stays_put: false`), and is often out of view. The piles at (439–441, 360) and (428, 366) and the gem at (429, 351) lie 5–15 cells from it, and the way to them runs past it. Detour checks the same thing (`states/detour.py:191`), and its walk only prices hostile reach as costly (`:219`). Four trips (Detour at 216 s, 228 s, 281 s and 322 s, and Gather's "gather → (440, 360)" and "(429, 351)") each ended in hits, 10 → 3 and then the death. Gather's pile target also ignored the named region: "gather → (440, 360) (region 416,384)". A hostile that has hit us should bar its last-seen post, and the route there, not just the target cell, for some time after it drops out of view.

   ```
   216 s  @430,381  detour → gem at (428, 366)       222 s hit (gristlewick)
   224 s  @430,372  gather → (440, 360)              231–233 s hit ×2, 9 → 7
   281 s  @434,382  detour → gem at (441, 360)       288–294 s hit ×3, 7 → 3
   322 s  @438,376  detour → gem at (429, 351)       326 s hit → 2; 346 s killed
   ```

3. **A `buy` op counts as done once any of the item is held** (A21). `goal_done` returns true for `buy` when a matching supply is held or stowed (`plan.py:771–772`). With 2 small_potions held, each `buy` small_potion finished at once: 8 times between 95 s and 300 s, each re-sent by the planner ("the potion count is still 2, so it did not complete"). Raising `potion_reserve` to 3 at 287 s changed nothing. A `buy` should be done when the count it found on top has gone up by one, or when the held count reaches `potion_reserve` for a potion.

**Minor:** Travel to town was given up twice mid-walk (at 3/10 near 316 s, after an escalation to Break at (429, 385), and at 540 s). Each time the Explore safe default then walked north toward the gristlewick. `explore_area` (399, 406) r12 finished at once 6 times. A Detour to a berry at (449, 370) alternated "stop, the step may land on a blocked cell" and one step for 12 decisions. The park found no reachable safe tile from (430, 378) and sent nothing for 60 s. Break armed matches at 2/10 beside the gristlewick.

### Run 6: killed 11 s in, at the spot where run 5's park gave up, and the park gives up again in a dead end

- **Code:** `main` at `1dfbe12`, after #161 (A24, A64, A21: the held-queue probe leaves Memory as it found it, Heal counts only rejected tries, a `buy` buys one more) and #155 (A72: State `signs_seen`, a discovery for an unread sign).
- **Verdict:** exit 0, `PASS` after **601.6 s**, on the short-run gates only. The park **timed out** after 60.4 s at (383, 369), with "Park: safe tile unreachable" on every decision, and cleared the queue (defect 1). The character started with **6/10 health**, 6 lives and **7 gems** at (430, 378), the cell where run 5's park timed out, 9 cells from the gristlewick. It had the pocket knife armed and held nothing. The local state directory was empty again.
- **Gate summary:** deaths **1**; API errors **0**; fights below the health floor 0; gems earned **yes**; armor **no**; shop weapon **no**; potion reserve **no**; Heal took ground food **no**, Heal drank a potion **no**. Planner: 47 calls, 43 plans accepted, 0 errors.

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–11 s | `travel` town, `explore_area` town r20; from 9 s `travel` point (424, 370), `read` ×3, `say` | The first decision, before any plan, queued a 10-step Explore walk north toward (415, 353), past the gristlewick and two snotlings. The plan applied at 1.8 s did not stop it. The gristlewick hit it at 4.8 s, and it was **killed at 10.7 s** (defect 2). |
| 11–63 s | `travel` point (424, 370), `travel` town, `read` ×3, `say`, `explore_area` | Respawned in town at 15.8 s, 10/10. Greet said hello to 11 NPCs, and 6 gave clues. The `travel` point the planner sent at 2/10 to get off the field was still on top, so Break cut 9 bushes from (413, 374) to (421, 370), back toward where it died. |
| 63–226 s | `travel` town, `read` ×4–7, `say` statue_carver, `gather_gems`, `buy` bronze_sword | Read all 13 signs, nearest first, walking up to 30 cells for some. A Detour to the (413–415, 414) pile at 85–98 s was broken off for the reads. |
| 226–417 s | `gather_gems` (region (384, 368) from 334 s), `buy` bronze_sword | 3 gems from cuts, at 254 s, 308 s and 415 s (**7 → 10**). From 372 s to 406 s Gather walked back and forth between a bush near (387, 369) and grass 4–9 cells east (defect 3). |
| 417–460 s | `buy` small_potion, `gather_gems` (368, 368), `buy` bronze_sword | Shop walked to the shop out of sight (`travel:shop`, from (382, 369) to (421, 399)) and bought one potion (**10 → 0**). The `buy` then finished. Detour took the (413–415, 414) pile (**+3**). |
| 460–600 s | `gather_gems`, `travel` town, `wait`, `travel` point (370, 384) | Gather and Retreat paced (369, 398) ↔ (370, 399) at 483–494 s. The guard fired and gave nothing up. Stuck detection gave up `travel` town at 507 s and the `travel` point at 528 s. Gather then cut near (368, 369–377) and at (384, 368) until the end. |

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **7** | **3** (+6 earned, −10 for one small_potion) |
| Armed | pocket_knife | pocket_knife |
| Worn | `{}` | `{}` |
| Held | none | small_potion ×1 |
| Potions | 0 | 1 |

Bought: one small_potion (10), by a `buy` op. Equipped: nothing; no `equip` op was sent. Drank: nothing. It held no potion before the death, and health stayed at 10/10 from the respawn to the end.

#### Gems earned

**6**: 3 from cuts ((388, 368) at 254 s, (375, 369) at 308 s, (383, 368) at 415 s; 73 cuts, none with no effect) and the (413–415, 414) pile (+3) at 456–460 s, by Detour. Cut yield was 0.04–0.06 gems per cut in the two regions it worked. The gem at (379, 377), on the farmer's "hollow rock", stuck a Detour at 236 s.

#### Deaths

**1** (lives 6 → 5), at 10.7 s, killed by the gristlewick (npc 217) near (424, 369), three hits of 2 at 4.8 s, 8.0 s and 10.7 s, 6 → 0. It held no potion: both of run 5's were lost at its death, and the one bought after it was drunk. So nothing could be drunk. Flee stepped one cell west at a time, and the gristlewick kept up.

#### Run 5 fixes, checked

| Run 5 defect | Run 6 | |
|---|---|---|
| Probe uses up Heal's drink tries (A24, A64, #161) | **Not tested.** It held no potion while hurt: none before the death, and the one bought at 443 s was never needed. 0 drinks were sent, so no try was counted or lost. | Run 5: both potions written off in 6 s. |
| A `buy` done once any is held (A21, #161) | **Holds; with stock, not tested.** 1 `buy` op, 1 purchase, 0 ops finished at once. It held no potion when the op came up, so a `buy` with stock in hand did not run. | Run 5: 8 ops finished at once. |
| Park timeout | **Not fixed.** Timed out again: 60.4 s at (383, 369), 0 steps after the first 2 (defect 1). #161 did not change it. | Run 5: 60.3 s at (430, 378). |
| Shop out of sight (A75, #157) | **Fixed.** The `buy` walked `travel:shop` from 35 cells off and bought at once. | Run 5: not tested. |

#### Signs (#155)

- **Seen: 13.** Sign discoveries fired as they came into view: 2 in the first trigger, and up to 11 unread at 61 s. `signs_seen` listed them unread first, with the read flag.
- **Read: 13 of 13.** Every `Read` returned its text as a Clues row of kind "sign": the eight gods, the four direction signs, and "Potions and food heal. Town is safe".
- **Plan changed: no.** The planner put `read` ops on top from call 2 to call 21 (64–226 s, about a quarter of the run), and dropped each one once it was read. No op came from what a sign said. It never set out for a temple, a garden or the ruins, and the stack stayed `gather_gems`, `buy` bronze_sword. Its notes picked up the statue clue ("count the statues, one faces away") but sent nothing for it.

#### Tokens

| | |
|---|---|
| Tokens | input 190,590, output 14,775, cache write 101,184, cache read 5,969,856 |

#### Decision mix

Decisions outside held queues: 65 no state (the park, and 3 planner waits), 71 Gather walks and 76 cuts (51 bush, 25 grass), 26 greetings, 16 Break (8 walks, 8 cuts), 16 Explore, 16 respawn syncs, 13 reads (6 read walks), 11 Retreat, 9 Travel, 9 Detour, 7 shop walks, 4 Flee, 4 `take gem`, 3 `take apple`, 1 Heal walk. Intents: 707 `Step`, 2,045 `Wait`, 84 `Use`, 26 `Say`, 13 `Read`, 9 `Take`. Call mix: 815 `tick`, 210 `entities`, 114 `zone`, 107 `strategist`, 66 `self`, 31 `terrain`, 16 `position`.

#### Top 3 defects

1. **Park walks into a dead end of the coarse corridor, then sends nothing for 60 s, because town is never ruled out.** The park began at (385, 367), with the gristlewick and two snotlings at (401–402, 368–369) in view. No safe tile was a candidate, so the goal was the town cell (397, 401), 34 rows south and outside the perception window. The direct A* hit its budget, so `cost_path` took the corridor branch (`navigation/planner.py:687–692`). Hostile reach priced the way east as costly, so the corridor ran south-west, macro (23, 23) → (24, 24) → (24, 25). `_fine_path` (`planner.py:505`) gave a 2-cell path to (383, 369), the cell with the best score in the window. From there no reachable cell beats where it stands, so `_fine_path` returns None (`:515–517`). `retreat_step` then sends nothing and says "safe tile unreachable" (`states/retreat.py:149–152`). It marks the goal only when `no_way` proves it walled off, and town is not: `no_way` is false. `no_progress` never rules out town (`retreat.py:114`), so each decision picks town again, plans the same corridor, and stands still. Replayed offline from the saved terrain, with the three NPCs in view and the gristlewick known hostile, it gives the same two steps and then "safe tile unreachable" on every decision. Without the three NPCs in view, Park walks east to town. Run 5's park timed out with the same message at (430, 378), 9 cells from the gristlewick, and run 6 started there and died in 11 s (defect 2). When `cost_path` gives no step toward town, Park should drop the corridor and take the best step of a plain fine search, or fall back to the nearest cell it has been safe on.

   ```
   600.8 s  @385,367  retreat → safe (397, 401)     Step(down_left)   corridor (23,23)→(24,24)→(24,25)
   602.6 s  @384,368  retreat → safe (397, 401)     Step(down_left)
   603.5 s  @383,369  no state (Park: safe tile unreachable)    × every decision to 660 s
   660.8 s  park timed out at 76:383,369 after 60.4s, queue cleared
   ```

2. **The first decision walks into a pack it does not know is hostile, and the plan that lands 1 s later does not stop it.** At 0.6 s, before any plan, the Explore safe default queued 10 steps north toward (415, 353), from (430, 378) to (429, 368). The gristlewick, whose pack was in view at the first `entities` read, stood 9 cells away when the walk was sent, and by 4.8 s it had moved to 1 cell east of the character. Which NPC types are hostile is learned per run (`WorldModel.hostile_types`, `world.py:246`, filled at `:691`). The knowledge base has an `npc_types` table (`knowledge_base.py:67`) that nothing writes. So the type that killed run 5 showed as `"hostile": false` in the planner's State. The planner's `travel` town was applied at 1.8 s, but a new plan never replaces a queue already sent (A71). The queue ran on, and the first hit came at 4.8 s, at 6/10 with no potion. Hostile types should be saved to the knowledge base, and a run that starts hurt beside an NPC pack should not send a walk toward it before the first plan.

   ```
   0.6 s   @430,378  explore → (415, 353)   10×Step 27×Wait     npc 217 gristlewick at (438, 369) in view
   1.8 s   plan replaced (travel town, explore_area town)        queue held
   4.8 s   @429,368  Attacked, Damaged 2 by npc 217             6 → 4
   10.7 s  Died                                                  lives 6 → 5
   ```

3. **Gather goes back and forth between a regrown bush and grass several cells off** (`states/gather.py:605–617`). Gather's pick takes the nearest stand beside a bush in the region before any grass, however far the bush is. A bush beside (387, 369) kept growing back at the region's west edge. So after each grass cut 4–9 cells east, the next pick walked back to that bush, and after the bush cut, the nearest grass was again to the east: 8 walks of 4–9 cells between 372 s and 406 s, 2 cuts per round trip. With a cut yield of 0.04–0.06 gems, the walking halved the cuts per minute. The pick should weigh walking distance against kind, so grass next to the character beats a bush 9 cells off.

   ```
   374.7 s  @387,369  cut grass
   376.0 s  gather → (391, 368)   4×Step    378.6 s cut grass, 380.0 s cut bush
   381.8 s  gather → (387, 369)   4×Step    384.3 s gather → (392, 368)   5×Step
   390.7 s  gather → (387, 369)   5×Step    393.4 s gather → (396, 368)   9×Step
   401.3 s  gather → (387, 369)   9×Step
   ```

**Minor:** The `travel` point (424, 370) that the planner sent at 2/10, to get the character off the field, came back after the respawn. Break then cut 9 bushes on the way back toward where it died (18–63 s). Gather and Retreat paced (369, 398) ↔ (370, 399) at 483–494 s, and the guard gave nothing up. Stuck detection gave up `travel` town at 507 s and a `travel` point 1 cell away at 528 s (a bush and a wall in the way). Greet said hello to 8 chugbugs and 4 snotlings. The first planner call went out before the first observation, so its State said `health=None/None gems=None`.

### Run 7: priced detours take two pile clusters, and a stale reply buys the matches twice

- **Code:** `main` at `76d3094`, after #159 (A73: Detour prices every find by the steps it adds, with an allowance per kind). #162 (Gather and Detour keep off known hostile ground) merged while it ran and is not in it.
- **Verdict:** exit 0, `PASS` after **601.7 s**, on the short-run gates only. The park **parked safe** at (399, 370) after 3.9 s and cleared the queue. The character started at 10/10, 5 lives and **3 gems** at (383, 369), the cell where run 6's park timed out, 18 cells west of the gristlewick and two snotlings. It had the pocket knife armed and held one small_potion.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **yes**; armor **no**; shop weapon **no**; potion reserve **no**; Heal took ground food **no**, Heal drank a potion **no** (Retreat drank it). Planner: 52 calls, 50 plans accepted, 1 error ("payload is not an object", at 195 s).

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–63 s | `travel` town, `read`, `say` farmer, `explore_area` town r20 | The first decision, before any plan, queued a 10-step Explore walk north, away from the pack this time. Travel walked to town. Greet said hello to 8 NPCs. |
| 63–140 s | `read` ×4–7 (statues and signs), `explore_area`, from 123 s `gather_gems:30` | Read the town signs and statues. Detour took the (413–415, 414) pile (+3). |
| 140–200 s | `read` ×1–3, `buy` matches, `gather_gems:30` | Bought matches at 182 s (**6 → 1**). The reply applied at 183 s had been asked at 181 s, before the purchase, and put `buy` matches back (defect 3). |
| 200–315 s | `gather_gems:30`, `buy` matches; from 268 s `gather_gems:15`, then `buy` matches on top | Cut 13 times west of town (+1), and Detour took the (359–361, 360) triple (+3). At 5 gems the second `buy` matches bought a second box (**5 → 0**). |
| 315–372 s | `gather_gems:25` | Cut 15 times east of town at (430–432, 390–398) (+1). At 361 s Gather picked the pile at (428, 366), 25 cells north, as the pack walked toward it (defect 2). |
| 372–411 s | `travel` town, `travel` point, `buy` small_potion, `gather_gems` | The gristlewick hit it 7 times. Retreat drank the potion at 7/10. Flee and Retreat then stepped back and forth for 19 s (defect 1), and Heal walked it out at 1/10. |
| 411–600 s | `wait` 30 s, `travel` town, `gather_gems:25` (368, 384) | Healing in town. Each time a `wait` ended, Gather set out for (428, 366) again, at 1/10, 3/10 and 5/10. Detours to two apples cut the first two walks short (+2 health each). The third reached (400, 367) at 600 s, beside the pack, and the park took it back to safe ground. |

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **3** | **1** (+8 earned, −10 for two matches) |
| Armed | pocket_knife | pocket_knife |
| Worn | `{}` | `{}` |
| Held | small_potion ×1 | matches ×2 |
| Potions | 1 | 0 |

Bought: matches twice (5 each), by `buy` ops. The second came from a stale reply (defect 3). Equipped: nothing; no `equip` op was sent. Drank: the small_potion, at 376.9 s at 7/10, while the gristlewick was hitting it. It was accepted on the first try.

#### Gems earned

**8**: 6 from piles, both clusters taken by Detour: (413, 414), (414, 414) and (415, 414) at 135–147 s, and (359–361, 360) at 264–267 s. 2 came from cuts, at (369, 386) at 226 s and (430, 395) at 338 s, out of 28 cuts (13 grass, 15 bush), none with no effect.

#### Deaths

**0**. The gristlewick (npc 217) hit it 7 times between 373.7 s and 387.4 s (1+2+2+2+2+1+2 = 12 damage), at (425–431, 368–371). Retreat drank the potion at 7/10, so health ended at 1/10, not dead. It came back to 3/10 and 5/10 from the two apples. In the park phase a snotling hit it once more, for 1.

#### Detours (#159)

| Find | Kind | Detour start | Walked | Straight line | Outcome |
|---|---|---|---|---|---|
| (413, 414), with (414, 414) | gem pile ×2 (+1 next to it) | (410, 406) at 126 s | 17 | 8 | Took 2. Pickup took the pile beside it. (415, 414) was left, 2 cells off. |
| (415, 414) | gem pile | (413, 417) at 143 s | 8 | 2 | Took it. The explore walk had already turned south. |
| (360, 360), with (359, 360) and (361, 360) | gem pile ×3 | (359, 372) at 259 s | 12 | 12 | Took all 3 in one stop. |
| (428, 366) | gem pile | (430, 381) at 366 s | 11 | 11 | Broken off: the gristlewick reached it 2 cells short. This was Gather's own target too. |
| (391, 385) | apple, hurt | (394, 400) at 492 s | 19 | 19 | Took it. |
| (393, 381) | apple, hurt | (399, 395) at 562 s | 17 | 17 | Took it. |

- **Count:** 6 detours: 4 to gems (3 finds), 2 to food while hurt. No detour to a life.
- **Taken vs passed:** 6 piles taken, in 2 clusters. Passed in view: the hollow-rock triple (377–379, 377), 6 cells off at closest, never priced (run 6 got a Detour stuck on it). The (439–441, 360) triple, 10 cells off, came in view during the fight. The (359–361, 440) triple was 22 cells off. The singles at (428, 366) and (429, 351) were beside the pack.
- **Extra steps:** the trace logs the find, not the price, so the extra steps each detour was priced at cannot be read back. Walked steps track the straight line, except the (413–415, 414) cluster: 25 steps for two stops 8 and 2 cells off. Detour aimed at (413, 414) and Pickup took its neighbour, so the third pile was a second detour. The two apples were 17–19 cells off in a straight line, against a 4-step food allowance. So the Gather walk out of town must have passed within a few cells of each. Detour should log the extra steps it priced, so the next run can check the allowances.

#### Run 6 fixes, checked

| Run 6 defect / #161 | Run 7 | |
|---|---|---|
| Probe uses up Heal's drink tries (A24, A64, #161) | **Holds.** 1 drink (Retreat, losing ground), accepted at once (`SupplyUsed`). No try was written off. | Run 5: both potions written off in 6 s. |
| A `buy` buys one more (A21, #161) | **Holds, with stock in hand.** The second `buy` matches ran with 1 held and bought a second, as specified. It was not wanted (defect 3). | Run 6: with stock, not tested. |
| Park dead end (known) | **Not hit.** The park started at (400, 367) with the pack 1 cell off and parked safe in 3.9 s. | Run 6: timed out at (383, 369). |

#### Tokens

| | |
|---|---|
| Tokens | input 197,055, output 15,252, cache write 101,204, cache read 6,173,444 |

#### Decision mix

Decisions outside held queues: 109 planner waits (healing in town, 420–593 s), 22 Gather walks and 27 cuts (15 bush, 12 grass), 26 greetings, 18 reads (5 read walks), 16 Explore, 15 Detour, 14 shop (9 buy, 5 walks), 12 Heal, 4 heal_measure, 4 heal in safe ground, 11 Flee, 9 Retreat, 10 Travel, 7 `take` (6 gem), 2 path resends, 1 re-arm. Intents: 675 `Step`, 1,864 `Wait`, 28 `Use`, 25 `Say`, 13 `Read`, 10 `Take`, 2 `Arm`. Call mix: 663 `tick`, 189 `entities`, 130 `zone`, 114 `strategist`, 58 `self`, 35 `terrain`, 21 `position`.

#### Top 3 defects

1. **Flee and Retreat step back and forth beside the gristlewick for 19 s, and it takes 6 hits** (`states/retreat.py:108`, `states/flee.py:120`). From 373.7 s Retreat's goal was the safe tile (428, 371). That is 2 cells from the gristlewick at (429, 369), and Retreat's path weighs no danger from the hostile it runs from. Flee stepped away from the gristlewick (west, then east), Retreat stepped back toward (428, 371), and neither reached it. "stop, the step may land on a blocked cell" at 374.9 s suggests the last step onto it was blocked. 11 Flee and 6 Retreat decisions between 373.7 s and 392.3 s, health 7 → 1 after the potion. `no_progress` did not rule the goal out inside its window. Heal's walk south to (405, 393) at 393.2 s got it out. Retreat should not pick a safe tile within reach of the hostile it runs from, or should rule one out after a hit there.

   ```
   373.7 s  @429,368  flee npc 217                         Step(down)
   376.5 s  @429,368  retreat → safe (428, 371)            Step(down_left)
   376.9 s  @428,369  retreat losing ground: arm and use small_potion
   378.9 s  @428,369  flee npc 217   → 379.5 s @427,369 flee → 380.2 s @426,368 retreat → safe (428, 371)
   383.7 s  @426,369  flee  → 384.8 s @427,369 flee → 385.6 s @428,369 flee → 386.6 s @429,368 retreat
   390.2 s  @431,371  retreat → safe (428, 371)    393.2 s  @430,372  heal_measure → (405, 393)
   ```

2. **Gather takes any gem pile it knows of before the region the op names, at any health and any distance** (`states/gather.py:597–602`). The op named the region (368, 384). Gather's pick still took the nearest pile in `w.entities`, (428, 366), 42 cells from town and out of sight. It is the pile beside the gristlewick pack's ground. It set out for it at 361 s (10/10), 489 s (1/10), 559 s (3/10) and 593 s (5/10). The first walk ended in defect 1's fight. Apple detours cut the next two short, and the last reached (400, 367) beside the pack as the run ended. The hostile part is #162, merged after this run. The rest stands: a named region should bound the pile pick too, and a `wait` that ends should not hand a 1/10 character to a 40-cell walk.

   ```
   361.3 s  @431,391  gather → (428, 366)                        10/10
   489.4 s  @390,408  gather → (428, 366) (region 368,384)       1/10
   559.7 s  @397,401  gather → (428, 366) (region 368,384)       3/10
   593.5 s  @393,381  gather → (428, 366) (region 368,384)       5/10; 600.3 s attacked at (400, 369)
   ```

3. **A reply asked before a `buy` finished puts the `buy` back, and buy-one-more turns it into a second purchase** (`strategist.py:1194–1209`, `plan.py:66`). Call 17 was asked at 181.0 s, with `held={"small_potion": 1}`. Shop bought matches at 182.5 s and the op finished. The reply was applied at 183.2 s, and it still held `buy` matches. Later calls saw `matches: 1` held and kept the op ("we hold 1 gem, so gather first"). At 314 s it bought a second box with its last 5 gems. A reply should drop ops that finished between its ask and its apply, and the planner should see that the op it re-sends is already done.

   ```
   181.0 s  strategist ask (call 17)      held={"small_potion": 1}
   182.5 s  @413,401  buy matches          gems 6 → 1, op done
   183.2 s  strategist applied (call 17)  read, buy matches, gather_gems:30
   314.3 s  @413,401  buy matches          gems 5 → 0   ("Matches bought (2 held)")
   ```

**Minor:** One planner reply at 195 s was not a JSON object. The next call recovered. The (413–415, 414) cluster took two stops: Detour aimed at one pile, and Pickup took only its neighbour. Greet said hello to 5 chugbugs, 4 snotlings and 2 gristlewicks.

### Run 8: baseline on the game update; grass pays, bushes do not, and the agent never swings

- **Code:** `main` at `7b16517`, with every fix from runs 4–7 (#159–#171). It is the first run on the game update: attack power 2 for everyone (about 65% to hit, knife 1–4 damage), grass gems at 20%/25%, bushes drop berries only, and a killed hostile may drop a gem (source: the Unreleased section of `CHANGELOG.md` in the game server repo, `evandelacruz/saims`). These facts replace `docs/GAME_NOTES.md` Gems and the knife's damage 2, which still describe the old game. The update to GAME_NOTES, and the agent's adaptation, are on the `c/game-update-1010` branch. The agent on `main` is not adapted yet, so this run is the baseline.
- **Verdict:** exit 0, `PASS` after **601.5 s**, on the short-run gates only. The park **parked safe** at (409, 418) after 0.3 s and cleared the queue. The character started at **4/10**, 5 lives and **1 gem** at (399, 370), where run 7's park left it. It had the pocket knife armed and held matches ×2 and no potion. The container was fresh, so the local state directory, and with it the world knowledge base, started empty: no hostile type or sighting from runs 4–7 was loaded.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; weak hostile kills **0**; gems earned **yes**; armor **no**; shop weapon **no**; potion reserve **no**; Heal took ground food **yes**, Heal drank a potion **no** (none held). Planner: 51 calls, 50 plans accepted, 1 error (not JSON, at 589.6 s).

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–67 s | `travel` town, `read` ×2, `explore_area` town r20 | Started at 4/10. Heal walked it into the safe zone and rested 15 s. Health did not rise. |
| 67–120 s | `read` ×4–6, `say` rumor_teller | Asked the rumor tellers and read signs. At 109 s Detour took it to the gem pile at (428, 366), 2 cells from the gristlewick pack, at 4/10 (defect 2). It took the pile and 3 hits, down to **1/10**. Retreat walked it out. |
| 120–200 s | `travel` town, `wait` 20 s, `read`, `gather_gems:30` | Back to town at 1–2/10. It greeted 9 townsfolk. |
| 200–290 s | `read` ×3–4 (statues), `gather_gems:10`, `buy` small_potion, `gather_gems:30` | Detour took the (413–415, 414) cluster (+3). Two apples and a berry brought it to 8/10. |
| 290–310 s | `buy` matches, `buy` small_potion, `gather_gems:30` (384, 370) | Bought a third box of matches at 305.6 s (**5 → 0**), on the rumor "Take matches. Lots." The potion buy then had no gems. |
| 310–440 s | `gather_gems:30` (384, 370), then `gather_gems:30 fight`, then `gather_gems:15`, `buy` bronze_sword | **0 cuts in 177 s.** Gather reported blocked by a hostile, and fell back to Explore's keep-away and explore walks (defect 1). |
| 440–511 s | `gather_gems:15`, `buy` bronze_sword | Cut 19 times at (359–388, 409–441) (+2). Detour took the (359–361, 440) triple (+3). At 511 s, 3 chugbugs hit it at once beside the cut, 8 → 5. |
| 511–590 s | `travel` town, `wait` 20 s, `gather_gems:15` (384, 416), `buy` bronze_sword | Retreat, then Travel, took it back to safe ground at 6/10. It set out to gather again at 553 s. |
| 590–600 s | none | A reply that was not JSON cleared the stack (defect 3). The safe default explored until the stop. |

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **1** | **5** (+9 earned, −5 for matches) |
| Armed | pocket_knife | pocket_knife |
| Worn | `{}` | `{}` |
| Held | matches ×2 | matches ×3 |
| Potions | 0 | 0 |

Bought: matches, by a `buy` op, once. Equipped: nothing. Drank: nothing; no potion was held.

#### Gems earned

**9** in 600 s, **0.9 a minute**.

| Source | Gems | Cuts | |
|---|---|---|---|
| Gem piles | 7 | | All by Detour: (428, 366) at 113 s, (413–415, 414) at 209–218 s, (359–361, 440) at 456–458 s. |
| Grass | 2 | 6 | (388, 430) at 442 s and (366, 409) at 505 s, 1 in 3. |
| Bushes | 0 | 13 | None of them could drop a gem. A 14th cut, Break's at (369, 426), dropped a berry, which it took. |
| Kills | 0 | | The agent swung 0 times. |

Cut grass and bushes grew back in about 60 s, as GAME_NOTES says. Gather cut only from 440 s to 511 s: 71 s of cutting in a 600 s run.

#### Fights

| Time | Where | Hostiles | Their swings | Hits | Damage | Ours | Outcome |
|---|---|---|---|---|---|---|---|
| 112–115 s | (428–429, 366–368), the pile beside the pack | npc 217, npc 216 (gristlewick pack) | 5 | 3 | 3 | 0 | Retreat walked out at 1/10. |
| 511 s | (365, 408), cutting | 3 chugbugs (npc 164, 167, 168) | 3 | 3 | 3 | 0 | Retreat, then Travel, walked out at 5/10. |

- **Count:** 2 encounters, both Retreats. Hostiles swung 8 times and hit 6 (75%, near the update's 65%), for 1 damage each.
- **Our hit rate:** none to measure. The agent never sent `Attack`. Its win estimate still assumes 1 damage a hit against 10 health and one swing a second (`survival.py:21–23`), so no fight looked winnable. A planner `gather_gems` with `fight: true` at 415 s met no fight either.
- **Kills and deaths:** 0 and 0. The lowest health was 1/10 at 115 s.

#### Runs 4–7 fixes, checked

| Fix | Run 8 | |
|---|---|---|
| Park dead end (#165) | **Holds.** Parked safe in 0.3 s at (409, 418). | Not a hard test: it started on safe ground. |
| One refuge for Retreat and Flee (#168) | **Holds.** 0 Flee decisions. All 4 Retreats walked straight out or ended at once: (403, 398) at 113 s, (399, 429) at 434 s, (397, 401) at 511 s and 538 s. | Run 7: 19 s back and forth, 6 hits. |
| Stale planner reply (#168) | **Holds.** One `buy` matches, at 305.6 s. Call 27 was applied at 304.0 s, before the buy, and no later reply put it back. | Run 7: a second box bought. |
| Persisted hostility (#162, #165) | **Not tested.** The knowledge base started empty. In the run, the pack was learned at 112 s, and Gather and Detour kept off it after that. | |
| Heal refusal and no-op drinks (#169, #171) | **Not tested.** No potion was held. | |

#### What the update changes for the agent

- **Bush cuts earn nothing now.** Gather treats grass and bushes alike (`states/gather.py:696–712`, from GAME_NOTES Gems). 13 of its 19 cuts were bushes, for 0 gems. Grass gave 2 in 6. Gather should cut grass only, and take bushes for food.
- **Fights are worth taking, but the agent cannot see it.** The hostiles hit 75% of swings for 1 damage. Our knife now does 1–4 at about 65%. The win model in `survival.py:21–23` is from before the update, so the agent retreated from 3 chugbugs at 8/10 and never swung. Kill drops stay out of reach until it changes.
- **The yield model needs to start again.** The region yields learned before the update (A63) count bush cuts and the old rates. The planner read "1 gem in 1 cut" at (384, 416) as the best region.

#### Tokens

| | |
|---|---|
| Tokens | input 144,328, output 12,271, cache write 101,586, cache read 5,079,300 |

#### Decision mix

Intents: 868 `Step`, 2,453 `Wait`, 23 `Say`, 20 `Use`, 13 `Read`, 9 `Take`, no `Attack`, no `Arm`. Call mix: 643 `tick`, 282 `zone`, 216 `entities`, 118 `strategist`, 52 `self`, 43 `terrain`, 16 `position`.

#### Top 3 defects

1. **Gather made no cut for 177 s, blocked by a hostile 10–13 cells off** (`states/gather.py:306–319`, `states/explore.py:86–119`). From 263 s to 440 s Gather made no cut. Its status said blocked by a hostile (the planner's notes read "blocked by a hostile for 62 s", then 153 s, then 171 s). With no cut to make, it fell back to Explore's keep-away ("hurt, hostile near: step away" at 6–8/10) and to explore walks. The hostile was a snotling 10–13 cells off, by the planner's notes, and grass lay all around town. The first cut came only once Gather had walked south to (388, 430). That cost about 30% of the run, at 1 gem in 3 grass cuts.

   ```
   256.6 s  @393,381  gather → (398, 369)
   263.8 s  @398,371  look for gems: hurt, hostile near: step away   (×4)
   273–433 s          look for gems: explore … / gather → (368, 411)
   415.6 s  strategist: gather_status blocked by a hostile for 153 s with no cuts
   440.8 s  @388,430  cut grass   (first cut of the run)
   ```

2. **Detour takes a hurt character to a gem pile beside a pack** (`states/detour.py:118`). At 109 s, at 4/10, Detour walked from the explore walk to the pile at (428, 366). It is the same pile beside the gristlewick pack as run 7's defect 2. The pack was not known as hostile yet (empty knowledge base), so nothing priced it. It took the gem and 3 hits, down to 1/10. A gem detour has no health gate: food has one (`hurt`), but gems are always worth the steps. Heal had also stopped resting at 4/10 after 15 s without a measured regen, so the explore walk ran at 4/10.

   ```
    82.1 s  @417,407  heal: rest in the safe zone   (last of 13, health 4/10)
   109.5 s  @430,375  detour → gem at (428, 366), then back to explore
   112.1 s  @429,368  Attacked npc 217, Damaged 1
   113.0 s  @428,366  Attacked npc 216, Damaged 1; gem taken (1 → 2)
   114.9 s            health 1/10
   ```

3. **A reply that is not JSON clears the whole stack** (`strategist.py:1323–1324`). At 589.6 s call 51 came back as "reply is not JSON: Extra data". Its goals were `[]`, and `_apply` made the empty plan the stack ("no valid goals; stack cleared"). That dropped `gather_gems` and `buy` bronze_sword, and the safe default explored until the stop. Run 7 had one bad reply too, at 195 s. A reply that cannot be read should leave the stack as it was.

   ```
   572.9 s  applied 50   gather_gems:15 (384, 416), buy bronze_sword
   589.6 s  deferred 51  invalid: reply is not JSON: Extra data: line 3 column 1 (char 523)
   590.8 s  cleared 51   goals []
   591.5 s  @414,401  explore → (443, 430)
   ```

**Minor:** The planner spent its only 5 gems on a third box of matches, on a rumor ("Take matches. Lots."), with 2 boxes held and health at 8/10. Greet said hello to 8 chugbugs (344–422 s). Three of them hit it at 511 s. Heal rested 15 s in the safe zone at 4/10 with no regen, then stopped. Health first rose from food at 143 s.

### Run 9: the first run with a kept knowledge base; Heal circles for 217 s, and remembered posts keep Gather off the grass

- **Code:** `main` at `17503b3`, after #172 (A81: the agent matches the game update; Gather cuts grass only, the win estimate prices our swing by the published roll, and the planner's arc counts kill drops) and #174 (run 8's notes). #175 (Gather stall, Detour risk gate, bad planner reply) is not in it.
- **Knowledge base:** kept from run 8 on the same machine, as a real user's would be. Run 8 started with it empty. At the start it held 3 hostile types (snotling, gristlewick, chugbug), 14 hostile sightings, each with a post, the town cell, 8 level entrances, 9 shop prices, 23 clues and 20 greeted NPCs. It held no regen answer and no damage per type. The gem-yield table was reset by #172's new format.
- **Verdict:** exit 0, `PASS` after **602.9 s**, on the short-run gates only. The park **parked safe** at (380, 405) after 0.3 s and cleared the queue. The character started at **6/10**, 5 lives and **5 gems** at (408, 419), where run 8's park left it. It had the pocket knife armed and held matches ×3.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; weak hostile kills **0**; gems earned **yes**; armor **no**; shop weapon **no**; potion reserve **no**; Heal took ground food **yes**, Heal drank a potion **no** (none held). Planner: 47 calls, 47 plans accepted, 0 errors.

#### Planner ops over time

| Time | Ops on the stack (top first) | What happened |
|---|---|---|
| 0–35 s | `travel` town, `gather_gems:15` | Heal took two apples, then it walked to town. It was at **10/10** by 27 s. |
| 35–50 s | `gather_gems:15` | Detour took the (413–415, 414) cluster (+3, 5 → 8). |
| 50–293 s | `gather_gems:15` (+ `travel` town 95–152 s), then `fight: false`, then `buy` bronze_sword | **No cut in 243 s.** Gather walked to grass at (430, 409), (436, 402), (377, 379) and (399, 430) in turn, and each time fell back to "look for gems: explore". The planner's notes give the reason as "blocked by a gristlewick to the south" and "the NE pack blocks that area" (defect 2). |
| 285–300 s | `travel` point, `gather_gems:15`, `buy` bronze_sword | Gather made its only cut, at (361, 394), 8 cells from the gristlewick's remembered post (npc 240). Flee stepped away at 285 s. At 299.7 s the gristlewick hit it for 2. Retreat walked it off. |
| 304–521 s | `travel` town, `gather_gems:15`, `buy` bronze_sword | **Heal circled for 217 s** at 9/10 to measure regen, in a 45-cell loop north-west of town, and never reached a safe tile (defect 1). The planner's `travel` town did not run. |
| 521–546 s | same | It reached the safe zone at (374, 395) and rested 20 s. Health did not rise. |
| 546–600 s | `gather_gems:15`, `buy` bronze_sword | Back to town, then to Gather. A Detour to an apple brought it to 10/10 at 592 s. No cut. |

#### Gear and gems

| | Start | End |
|---|---|---|
| Gems | **5** | **8** (+3 earned, nothing spent) |
| Armed | pocket_knife | pocket_knife |
| Worn | `{}` | `{}` |
| Held | matches ×3 | matches ×3 |
| Potions | 0 | 0 |

Bought, equipped and drank: nothing. `buy` bronze_sword waited for 15 gems from 215 s.

#### Gems earned

**3** in 600 s, **0.3 a minute**: all from the (413, 414), (414, 414) and (415, 414) piles at 45–49 s, by Detour and Gather. Grass: **1 cut** (at (361, 394), 293 s), no gem. Kills: 0. Break cut two bushes on walks (126 s and 331 s), and the first was filed as a gem-yield cut.

#### Fights

| Time | Where | Hostile | Started by | Their swings | Hits | Damage | Ours | Outcome |
|---|---|---|---|---|---|---|---|---|
| 285–300 s | (355–368, 394–402), beside its post | npc 240 (gristlewick) | it | 1 | 1 | 2 | 0 | Flee, then Retreat; then Heal (defect 1). |

- **Count:** 1 encounter, started by the hostile. The agent never sent `Attack`, so the new win estimate (#172) never went into a fight.
- **Planner:** set `fight: false` on `gather_gems` from 186 s to the end ("with 10 health and 5 lives, a snotling fight isn't worth the risk yet"). The progression arc says an unmeasured type is refused, and every type was unmeasured at the start (defect 3).
- **Kills and deaths:** 0 and 0. A hit now does 2 damage (run 8: 1).

#### What the kept knowledge did

| Knowledge | Used? | |
|---|---|---|
| Town cell, shop prices, entrances | **Yes.** All three were in call 1's State, before the first sync. | The first plan was `travel` town, then `gather_gems:15` toward the bronze sword (15). |
| Hostile types | **Yes.** The snotlings, gristlewicks and chugbugs were hostile from the first decision. | No `Say` was sent (run 8 greeted 8 chugbugs). |
| Hostile posts | **Too much.** Gather kept off ground near 14 remembered posts around town, and made 1 cut (defect 2). | The one cut, at (361, 394), still drew a hit from the gristlewick beside its post. |
| Clues | **Read, not acted on.** The planner named the farmer's hollow rock (about (379, 375)) and left it until it had a mallet. | |
| Regen | **None was saved.** Heal set out to measure it at 9/10 (defect 1). | |

#### Known defects (#175 not in yet)

| Defect | Run 9 | |
|---|---|---|
| Gather stalls near a distant hostile | **Shows, worse.** 1 cut in 600 s, no cut from 50 s to 293 s. | Run 8: no cut for 177 s. |
| Detour at low health | **Did not show.** Detours ran at 10/10 (piles) and 9/10 (apple). | |
| Bad planner reply wipes the stack | **Did not show.** 0 invalid replies. | |
| Persisted hostility (#162, #165) | **Works,** and it now holds Gather off too much (defect 2). | Run 8: not tested. |
| Park | **Holds.** Parked safe in 0.3 s. | |

#### Tokens

| | |
|---|---|
| Tokens | input 160,792, output 8,325, cache write 103,194, cache read 4,746,924 |

#### Decision mix

Intents: 1,075 `Step`, 2,909 `Wait`, 4 `Take`, 3 `Use`, no `Say`, no `Attack`. Call mix: 578 `tick`, 439 `zone`, 249 `entities`, 100 `strategist`, 52 `terrain`, 46 `self`, 8 `position`.

#### Top 3 defects

1. **Heal walks 217 s at 9/10 to measure regen, round safe tiles it cannot reach** (`states/heal.py:109–116`, `states/heal.py:194–218`). After the hit, health was 9/10 and the knowledge base had no regen answer, so Heal took `heal_measure` and walked for the safe zone. Each goal was a safe tile at (389–392, 393–394). The walk gave one up, then took the next one beside it, which was just as unreachable. The planner read it as "Heal-measure targets are unreachable (bush blocks the path)". It went round the same loop north-west of town about 4 times, from 304 s to 521 s, and its `travel` town op could not run. One rest in the safe zone at the end (20 s) still gave no regen answer. Two fixes are needed. A goal Heal gives up on should rule out the tiles beside it, or fall back to town. And measuring regen should not hold a 9/10 character off the plan for minutes.

   ```
   304.5 s  @361,395  heal_measure → (389, 393)
   352.6 s  stuck: lapsed (397, 401)
   363.0 s  @383,366  heal_measure → (389, 397)   397.4 s  @395,359  → (391, 393)
   421.1 s  @358,382  → (389, 394)    444.8 s  @397,354  → (392, 393)   492.8 s  @396,354  → (390, 393)
   468.6 s  @354,381  → (389, 393)    515.6 s  @361,383  → (389, 394)
   526.3 s  @374,395  heal: rest in the safe zone   (20 s, health 9 → 9)
   ```

2. **Remembered posts wall off the grass around town, so Gather barely cuts** (`hostile_memory.py:37–117`, `states/gather.py:306–319`). The knowledge base loads every remembered sighting with its post. Run 8 left the chugbug pack's post south-west of town, the gristlewick and snotlings north-east, a snotling east and a gristlewick west. With all of them priced as hostile ground, Gather found no grass it would cut for 243 s. It went back to explore walks between town and the posts. Only 1 cut in the run, against 19 in run 8, and run 8 started with no posts known. #175's stall fix should be checked against a kept knowledge base like this one: remembered posts are the worst case for it.

   ```
    50.0 s  @415,414  gather → (430, 409)       77.9 s  look for gems: explore → (445, 411)
   152.6 s  @397,401  gather → (436, 402)      158.2 s  look for gems: explore → (357, 388)
   164.1 s  @396,401  gather → (377, 379)      181.7 s  look for gems: explore → (357, 388)
   186.1 s  strategist: "Gather is blocked by a gristlewick to the south"
   293.1 s  @361,394  cut grass   (the only cut)
   ```

3. **Measured damage per type is not saved, so each run starts with every type unmeasured, and no fight starts** (`hostile_memory.py:68–75`, `threat.py:34`). `save_hostiles` writes `hostile: true` per type and the sightings, but not `w.threat.by_type`. Run 8 measured all three types (1 a hit), and run 9 started with none. The planner's progression arc (`strategist.py:204`) refuses an unmeasured type, so its `gather_gems` kept `fight: false` all run. #172's new win estimate never went into a fight, and kill drops stayed out of reach.

**Minor:** A `Take` of an apple at 222.6 s was refused `target_not_nearby`, and Heal walked to it instead. Break cuts on walks are filed as gem-yield cuts: the bush at (402, 437) is in `gem_yield`. Gristlewick 240's remembered post moved from (369, 400) to (357, 400).
