# A67 live free-play runs with the planner on (redacted)

Scenario: free play. `scripts/smoke_m8_olympuff.py --seconds 600` with the AI planner on, on a copy of `python/characters/olympuff_m8.toml` whose directives file holds no goals (`goals = []`), so the planner decides everything. Deaths do not end the run (no `--stop-on-death`), and the 60 s park phase runs after it. The character is chosen at run time, and no name or id is committed. It started with about 21 gems, so the progression arc (`strategist.py:176`) should take it to stage 2: buy and equip gear, then go on hunting gems.

## Free play

### Run 1: an accidental sword, then 380 s frozen in a Break loop that swaps weapons

- **Code:** `main` at `5ff4f0f` (after #133 A64 park and #134 A63 run 3 fixes).
- **Verdict:** exit 0, `PASS` on the M8 survival gates after **600.9 s**. The park timed out after 60.3 s, still at (362, 377), and cleared the queue. The character started at 7/10 health, 7 lives and 21 gems, armed with the pocket knife, on the overworld 17 blocks north-east of the town cell.
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

**0.** 6 cuts took effect (3 grass, 3 bush), 0 `applied_no_effect`, no gem drops; `gather_run={"cuts": 6, "gems_gained": 0}`. The 380 s freeze cost the rest of the hunt.

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

1. **Break flips Arm between two weapons forever and never steps.** At 231 s the walk to the planner's (370, 370) reached stuck step 2. From then on every Break decision sent one `Arm`: knife (2350), then sword (20272), then knife again, 288 times over 370 s, at the same cell. It never stepped, never used a block, and never escalated. The planner, Park (`safe tile unreachable`) and the M8 gates all missed it.

   ```
   t=4045761 @76:362,377 tick  Arm (break → (365, 380))    supply 2350
   t=4045778 @76:362,377 tick  Arm (break → (365, 380))    supply 20272
   t=4045791 @76:362,377 tick  Arm (break → (370, 370))    supply 2350
   t=4045813 @76:362,377 tick  Arm (break → (370, 370))    supply 20272
   …  (288 in all; State flips armed=pocket_knife / bronze_sword between planner calls)
   t=4050044 @76:362,377 park  park timed out at 76:362,377 after 60.3s
   ```

   Suspects: `break_memory.py:157` picks a tool from `w.held_supplies` only, and that list leaves out the armed weapon. When two held weapons share the capability, the weapon not armed always wins, so each Arm makes the other weapon the choice. `held_supplies(w)` at `:112` already adds the armed one back. `states/break_state.py:89–123` returns `[Arm, SetPosition]` unpaced, and `brain.py:142` sends only the first intent, so the step never goes out. The stuck clock (`break_state.py:116`) escalates only when there is no step, and here there was one every time.

2. **`travel to: shop` walks onto a shop item and buys it.** With no x, y, Travel resolves `shop` to the nearest shop cell (`travel/resolve.py:68`, cells from `travel/knowledge.py:129`), and that cell is the item for sale. The walk ended on (414, 398) and took the bronze_sword, 15 of 21 gems, while the planner's only `buy` ops named small_potion. That left 6 gems: no potions and no armor, and stage 2's readiness was out of reach for the rest of the run.

   ```
   call 13  travel to=shop x=0 y=0, buy small_potion, buy small_potion   "Buy potions first"
   t=4044916 @76:414,398 tick  — (queue held) | SupplyTaken 20272   gems 21 → 6
   call 14  "Can't afford potions at 6 gems; gather first, then buy"
   ```

   Travel should stop next to a shop cell, never on one; buying is Shop's job. Earlier, Shop's own walk to (422, 398) never arrived: it started seven times between 69 s and 132 s, four of them from (406, 392), where oscillation gave up an explore target.

3. **The planner cannot see a stall, and churns on the 0, 0 placeholder instead.** The State has no line saying the character has not moved, or that the top op's executor is stuck in a loop. For 370 s the planner saw `pos=76:362,377` with a `travel` (370, 370) on top and kept the stack ("Keep the retreat on top"). Its replies instead fixed something that was not broken. `plan.py:157` turns a symbolic `travel` with no x, y into 0, 0, and `strategist.py:656` prints that back in the stack. 22 replies said they had "dropped the placeholder 0,0" and re-sent the same op, which the plan filled with 0, 0 again. At 234 s the same doubt ("The travel target of town at x=0,y=0 looked odd") is why it swapped `travel` town for `travel` (370, 370), the walk that led into defect 1.

   ```
   call 20  "The travel target of town at x=0,y=0 looked odd, so I replaced it with a nearby safe tile."
   call 27  "Removed the placeholder 0,0 from the town travel."
   call 36  "Dropped the invalid 0,0 coordinates on the town travel so it uses the known town."
   call 44  "Keeping the retreat on top."   pos=76:362,377 since 231 s
   ```

   Suspects: `strategist.py:509–525` (no time-at-cell or top-op progress line, no entrances) and `strategist.py:656` (shows the 0, 0 the planner never sent). Omitting x, y for a symbolic `to` when they are 0, 0, and adding something like `stalled_at_cell_s` with the top op's last decision, would let the planner see this stall.

**Minor:** call 1 ran before the first `self` and inventory reads (`gems=None`, `health=None/None`, `armed=None`), so the run's first plan was made blind. `nearest_safe` pointed at (370, 370), but every zone read near it came back `safe=False`. Greet said hello to 6 snotlings and gristlewicks, monster-looking types that had not yet hit anyone this run, so they counted as not hostile.
