# A63 live gem-gathering runs with the planner on (redacted)

Scenario: gather N gems. `scripts/smoke_m8_olympuff.py --seconds 300` with the AI planner on, on a copy of `python/characters/olympuff_m8.toml` whose directives file holds one goal, `goals = ["gather_gems:20"]` (the directives shorthand, `plan.py:86`). The directive pins `gather_gems` on top of the stack; the planner plans below it. Character chosen at run time; no name or id is committed. Survival judged only (M8's milestone clauses need a full hour).

## Run 1 — no cuts in 300 s: Gather never finds ground it may cut

- **Code:** `main` at `0ea72cb` (after #116 gem yield by region and #117).
- **Verdict:** exit 0, `PASS` on survival after **301 s**. Character started at 10/10 health, 9 lives, 1 gem, on the overworld 213 blocks east of the town cell.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **no**; armor, shop weapon, potion reserve, heal takes all **no**.

### Gems and gem yield

| | |
|---|---|
| Gems | **1 → 1** |
| Cuts (`Use` on grass or bush) | **0** |
| `Take` | 2, both a berry (not a gem) |
| `gem_yield` records | **none**: `kb.extra["gem_yield"]` was never written, so no region totals and no barren marks |

The map was not short of targets: the knowledge base ended with 31,120 known cells on map 76, grass and bushes among them from the first terrain read (e.g. grass at (585, 371), a bush at (588, 371)).

### Planner

| | |
|---|---|
| Calls | 20 (17 `applied`, 3 `unchanged`) |
| Plans accepted / errors | **20 / 0** |
| Tokens | input 10,606, output 3,880, cache write 98,054 (call 1), cache read 1,863,026 |
| Tokens per minute (budgeted) | **~101k in minute 1** (the one cache write), then **~2.7–3.3k/min** |

Two replies (calls 17 and 19) wrapped the JSON in prose, and call 19 in a stray `<invoke name="x">` tag; both still parsed and applied.

### Decision mix (76 decisions that sent intents; 176 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| gather_gems (pinned): `look for gems: explore →` (Gather's fallback to the safe default) | 64 |
| flee | 8 |
| take berry (Loot) | 2 |
| heal_food | 1 |
| path stale, resend | 1 |
| cut grass, cut bush, take gem | **0** |

Intents: 531 `Step`, 1,391 `Wait`, 2 `Take`, 0 `Use`. Call mix: 252 `tick`, 212 `zone`, 40 `strategist`, 33 `terrain`, 21 `self`, 11 `position`, 4 `entities`.

### Outcomes

- **Deaths:** 0 (lives 9 → 9). Health 10 → 7: npc 213 hit for 1 and 2 at (424, 321) around 110 s, npc 220 for 1 at (341, 352) at 180 s. Flee got clear both times.
- **Stalls > 10 s at one cell:** none. The character was moving all run: (610, 396) → (424, 321) → (344, 362) → (265, 428), about 345 blocks west.
- **Left barren or low-yield areas on its own:** **not exercised.** With no cut filed there was no yield to act on. The long walk west is Gather's explore fallback, not a yield decision.
- **All 212 zone reads came back `safe=False`**, and the walk never came within 12 blocks of the town cell (397, 401).

### Top 3 defects

1. **Gather cuts only on known safe-zone cells or within 8 of town, so in the field it never cuts.** `is_safe_ish` requires the cell to touch a known safe zone or lie in the respawn probe ring; every zone read was unsafe and the character never reached town, so `cuttable` was false for every grass and bush in view. Gather then hands each decision to the safe default.

   ```
   t=3944278 @76:610,396 tick  queue 10×Step 27×Wait (look for gems: explore → (585, 385))
   t=3944491 @76:571,373 tick  queue 10×Step 28×Wait (look for gems: explore → (558, 362))
   t=3947121 @76:279,444 tick  queue 10×Step 27×Wait (look for gems: explore → (265, 428))
   gems: start 1, max 1, earned: False
   ```

   Suspect: `states/gather_safe.py:53` (`return touches_safe_zone(w, pos) or near_respawn_anchor(w, pos)`). It contradicts the planner's own prompt, `strategist.py:164` ("cut grass and bushes … field work makes about 3 gems a minute"), and starves A63, which can only learn yield from field cuts. A hostile-free, hazard-free cell would be a fair bar for grass.

2. **Gather's fallback explores away from the only ground it may cut.** With nothing cuttable in view, `GatherState.act` calls `safe_default`, which pushes to the nearest frontier outside safe ground. That led 345 blocks west, never toward the town cell the knowledge base already held. 64 of 76 decisions were this fallback.

   Suspect: `states/gather.py:51–53`. When Gather's bar is "near town or a safe zone", its fallback should head for town or the nearest known safe tile, not for the frontier.

3. **The planner spends every call retargeting `gather_gems` x, y to the region it stands in, and that op never runs.** 15 of 17 applied plans differ only in the x, y of the planner's own `gather_gems`, chasing `gem_yield.here` as the character walks ("The previous plan named region 544,352, but the character stands in region 512,352"). The op sits below the pinned directive `gather_gems`, so it never reaches the top; and x, y only lifts a barren mark (`plan.py:67`), it does not move the character. The planner cannot see why Gather is not cutting, so it guesses ("gem_yield has no measured regions yet, so gather_gems has nowhere to start").

   ```
   call 4  gather_gems x=544 y=352   "sample the current region; no measured yields yet, so name it explicitly"
   call 5  gather_gems x=512 y=352   "the character stands in region 512,352"
   call 6  gather_gems x=480 y=352   "the previous plan's 512,352 was wrong"
   …
   call 20 gather_gems x=272 y=432   "State gem_yield.here is (272,432), not (288,432)"
   ```

   Suspects: `gem_yield.py:251` puts `here` in the summary with no cuts behind it, which invites naming it, and `strategist.py:168` frames x, y as where Gather works. The State gives no reason for a gather that sends no cut (e.g. "nothing safe-ish to cut in view").

**Minor:** the spurious `idle` trigger from Walk run 3 is still there. Call 2 fired 4 s in with `{"trigger": "idle", "since_tick": 0, "tick": 3944265, "idle_ticks": 6000}`, and the planner wrote "Idle 6000 ticks: gather_gems on top appears stuck" into its notes. `runner.py:270` seeds `strategist_progress_tick` from `self.world.tick` before any tick is known (0); `strategist.py:708` then fires on the first real tick.

## Run 2 — 168 cuts on one town cell, every one `applied_no_effect`: town grass does not cut

- **Code:** `main` at `e193ef9` (after #121 Gather cuts in the field).
- **Setup:** as Run 1: `gather_gems:20` directive, planner on, `--seconds 300`. The local knowledge base started empty (fresh checkout), where Run 1's held 31,120 cells.
- **Verdict:** exit 0, `PASS` on survival after **301 s**. Character started at 10/10 health, 9 lives, 0 gems, on the overworld 10 blocks north of the town cell (inside town).
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **no**; armor, shop weapon, potion reserve, heal takes all **no**.

### Gems and gem yield

| | |
|---|---|
| Gems | **0 → 0** |
| Cuts (`Use` on grass or bush) | **168 sent, 0 took effect**: every `Use` was on grass at (387, 387), the cell the character stood on, in town |
| `Take` | **0** |
| `gem_yield` records | **none**: `kb.extra["gem_yield"]` was never written, so no region totals and no barren marks |

A probe after the run, same character and cell, read the 3×3 around (387, 387) as grass with `safe: true` and got `applied_no_effect` for `Use` on (387, 387) and on the neighbour (388, 388); both stayed grass. The API reference says the knife "cuts field grass and bushes" and that gems drop from cutting "outside town" (`agentrealm_reference.md:1511`, `:2424`).

### Planner

| | |
|---|---|
| Calls | 20 (1 `applied`, 13 `unchanged`, 6 `kept`) |
| Plans accepted / errors | **14 / 0** (script summary) |
| Tokens | input 9,038, output 1,865, cache write 98,370 (call 1), cache read 1,869,030 |
| Tokens per minute (budgeted) | **~100.6k in minute 1** (the one cache write), then **~2.1–2.2k/min** |

**Churn: none.** Call 1 put `explore_area` (395, 391) r30 below the pinned `gather_gems` and dropped a `travel to shop` for missing `x`. Calls 2–20 re-sent the same `explore_area` or an empty goals list. No call touched `gather_gems` x, y (Run 1's defect 3 is fixed). Every State from call 2 on carried `gather_status="cutting"`, and every reply read it as progress ("Pinned gather_gems is still cutting, so it stays on top").

### Decision mix (170 decisions that sent intents; 194 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| gather_gems (pinned): `cut grass` at (387, 387) | 168 |
| gather_gems (pinned): `gather → …` (walk to a cell to cut) | 2 |
| explore fallback, flee, loot, heal | **0** |

Intents: 16 `Step`, 603 `Wait`, 168 `Use`, 0 `Take`. Call mix: 364 `tick`, 307 `zone` (306 `safe=True`), 40 `strategist`, 26 `self`, 1 each `world`, `position`, `terrain`, `entities`.

### Outcomes

- **Deaths:** 0 (lives 9 → 9). Health 10 on every self read; no `Damaged` events.
- **Stalls > 10 s at one cell:** **one, ~290 s.** The character reached (387, 387) at 10 s and stood there to the end. Gather had set out for (398, 369), but the first decision on grass took the `cut grass` branch for the cell underfoot and never moved again.
- **Left barren or low-yield areas on its own:** **not exercised.** No cut was filed, so there was no yield to act on, and the one cell never went exhausted.
- Run 1's spurious `idle` trigger did not fire (only `map` and `timer` triggers).

### Top 3 defects

1. **Gather cuts town grass, which the server never cuts.** #121 dropped the safe-zone bar entirely; `gather_ground` now accepts any known cell off hazards with no hostile near, town included. The character starts in town, stands on grass, and cuts it forever.

   ```
   t=3965718 @76:397,397 tick  queue 10×Step 27×Wait (gather → (398, 369))
   t=3965768 @76:387,387 tick  Use(block:) (cut grass)
   t=3965801 @76:387,387 tick  Use(block:) (cut grass)
   …
   t=3968656 @76:387,387 tick  queue 0×Step 7×Wait (cut grass)
   gems: start 0, max 0, earned: False
   probe: Use (387,387) → applied_no_effect; terrain g = {"block_type": "grass", "safe": true}
   ```

   Suspects: `states/gather_safe.py:29–39` (`gather_ground` never excludes a known safe-zone cell; terrain reads carry `safe` per cell and `touches_safe_zone` at `:51` already knows them), and `strategist.py:170`, which tells the planner Gather cuts "in the field as in town". Field grass is the only grass worth cutting: exclude safe-zone cells, and head out of town when none is left.

2. **An `applied_no_effect` cut is not learned, so the same cell is retried without end.** The runner files a cut only on `outcome == "applied"`. A no-effect `Use` neither reaches `gem_cuts.note_cut` nor marks the cell exhausted, so `exhausted_cells` never skips it and Gather picks it again next decision: 168 times on one cell, and no `gem_yield` record.

   Suspect: `runner.py:843`. A no-effect `Use` on grass or a bush should mark that cell exhausted (or uncuttable for the run) so Gather moves on; it should not be filed as a gemless cut, or town would turn barren.

3. **`gather_status` says "cutting" while nothing is cut, so the planner sees progress.** `gather_outcome` sets `CUTTING` whenever it returns intents, whatever their result. 19 straight planner calls read "cutting" with gems at 0 and changed nothing; 13 were `unchanged` re-sends of the same `explore_area`.

   ```
   call 9   "Pinned gather_gems(20) is cutting and gems are still 0. Keep the town exploration below it"
   call 14  "Pinned gather_gems is cutting and gems are at 0, so keep it on top."
   call 20  "Pinned gather_gems is still cutting and stays on top."
   ```

   Suspect: `states/gather.py:98–99`. The status should reflect results (e.g. "cuts have no effect here" after a no-effect `Use`), and the State could carry cuts and gems this run so the planner can tell work from a stall.

**Minor:** call 1's `travel` `to: "shop"` was dropped for missing `x` (`plan.py:111` requires x, y for every `to`; the op schema at `plan.py:58` lists them, but a shop the character has not found yet has no cell). Zone reads took 307 of 741 calls, 306 of them `safe=True` town cells, while the character stood still.

## Run 3 — 11 gems in 168 s, then a flee step into known lava: a stale position aims the Step

- **Code:** `main` at `e993602` (after #125 survival fixes, #126 Gather cuts field cells first and learns no-effect cuts, #127 B133 `finished_queue`), against the server redeployed with B133 and sim changes.
- **Setup:** as Run 2: `gather_gems:20` directive, planner on, `--seconds 300`, empty local knowledge base.
- **Verdict:** exit 1, `FAIL` after **168 s**: one death, and the smoke ends a run at its first death (`acceptance_run.py:42`). Character started at 10/10 health, 9 lives, 0 gems, on the overworld 216 blocks south of the town cell, 8 blocks from a known safe zone (395, 609) and its shop supplies.
- **Gate summary:** deaths **1**; API errors **1** (`position` 409 `not_on_map`, read one tick after the death); fights below the health floor 0; gems earned **yes**; armor, shop weapon, potion reserve, heal takes all **no**.

### Gems and gem yield

| | |
|---|---|
| Gems | **0 → 11** (6 by 71 s, flat to 149 s, 5 more by 163 s) |
| Cuts (`Use` on grass) | **28 sent, 28 took effect, 0 `applied_no_effect`**; no bush cuts |
| `Take` | 22 sent for 12 supplies, 11 taken: 6 gems our cuts dropped, 3 from an authored pile cluster at (386–388, 535–536), 2 apples. The Take of pile gem 458 got no result. Two more gems (459 in the cluster, 524 at (400, 529)) were picked up by walking onto them. **10 of the 11 took a second, wasted Take** (below) |
| `gem_yield` records | 28 cuts filed. Region (400, 592): **22 cuts, 6 gems, yield 0.27**. Region (400, 576): **6 cuts, 0 gems**. Barren marks **none** (30 cuts needed); uncuttable marks **none** |

All cuts were field grass (the one zone read near the cuts, (406, 600), was `safe=False`). Run 2's defects 1 and 2 are fixed: no town cut, no no-effect cut.

### Planner

| | |
|---|---|
| Calls | 11 (1 `applied`, 6 `kept`, 4 `unchanged`) |
| Plans accepted / errors | **5 / 0** (script summary) |
| Tokens | input 6,444, output 1,560, cache write 99,234 (call 1), cache read 992,340 |
| Tokens per minute (budgeted) | **~99.8–101.9k in minute 1** (the one cache write), then **~2.9–3.1k/min** |

**Churn: none.** No call touched the pinned `gather_gems`. Call 10 put a shopping chain under the pin (`travel` to shop, `buy` small_potion, bronze_sword, bronze_mail, `equip`); call 11 re-sent it. Run 2's dropped `travel to: "shop"` is fixed (a symbolic `to` without x, y now becomes 0, 0, `plan.py:156`). The State's `gather_run` counts worked: calls 3–7 read the rising cuts and gems correctly ("18 cuts gave 6 gems, a yield of 0.33 per cut"). From call 9 on, `gem_yield.best` and `here` were empty: the character had walked more than 3 regions from (400, 592) (`SUMMARY_RADIUS`, `gem_yield.py:61`).

### Decision mix (112 decisions that sent intents; 109 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| gather_gems (pinned): `gather → …` (walk to a cell or pile) | 60 |
| gather_gems (pinned): `cut grass` | 28 |
| gather_gems (pinned): `take gem` | 19 |
| take apple (Loot) | 3 |
| flee | 2 |
| explore fallback, heal, fight | **0** |

Intents: 130 `Step`, 211 `Wait`, 28 `Use`, 22 `Take`. Call mix: 221 `tick`, 58 `entities`, 28 `position`, 22 `strategist`, 15 `self`, 7 `terrain`, **3 `zone`** (Run 2: 307), 1 `world`.

### Outcomes

- **Left town for field ground:** yes. It never stood in town; from its start beside a southern safe zone it walked straight to field grass at (406, 600) and cut there from 11 s.
- **Left low-yield regions on its own:** **not by yield.** It cut 22 times in (400, 592) for 6 gems, then 6 times in (400, 576) for none, then stopped cutting and walked 55 blocks north. That walk was a hostile shadowing it (defect 2), not a yield decision; no region reached a barren mark.
- **Deaths:** **1** (lives 9 → 8) at 163 s, at (401, 528): 12 `occupy` damage from **lava**, after a 2 from npc 264 (gristlewick). Health was 10 on every self read before.
- **Stalls > 10 s at one cell:** none (longest 6.4 s, taking a pile at (386, 536)). But **cutting stalled for 45 s** (104 s → 149 s): 0 cuts, gems flat at 6.

### B133 and the new server

- `finished_queue` held walk and cut queues as intended: 109 ticks held a queue, no lost-queue resets, no position re-reads from a lost handoff.
- **A lone `Take` is not tracked as a queue, so it is decided again before it has run.** Each `Take` was answered with no result, the next decision (3 ticks later) sent the same `Take`, and its response carried the first one's `SupplyTaken`:

  ```
  t=4004401 @76:406,595 tick  Take(62583) (take gem)
  t=4004404 @76:406,595 tick  Take(62583) (take gem) | SupplyTaken 62583 @ tick 4004401
  ```

  20 of the 22 Takes were such pairs, one wasted call per supply. Suspect: `runner.py:690–693`, where a non-walk, non-`Use` intent sets `pending_intents = None`, so the response's `queue` is not held and `apply_finished_queue` (`:797`) never waits on it.
- No other new-server behavior seen: `Damaged` events carry `source_kind` (`npc`, `occupy`) as documented, and lava reads `occupy_damage: 12`, fire 2.

### Top 3 defects

1. **Flee re-sent a Step from a stale position and walked into known lava.** At 4005715 Flee queued `Wait×3, Step up_right` from (399, 530) to (400, 529) (dirt). The step ran at about 4005718. The next decision still held (399, 530) as the position, so its `Step up_right` toward (400, 529) went from (400, 529) to (401, 528), lava, in the knowledge base since the terrain read at 4005526. Health 8, lava 12.

   ```
   t=4005715 @76:399,530 tick      queue 1×Step 3×Wait (flee npc 264)
   t=4005716 @76:399,530 position  (399, 530)
   t=4005721 @76:400,529 tick      Step(up_right) (flee npc 264) | Attacked, Damaged 2 by npc, SupplyTaken
   t=4005723             self      lives=8 alive=False health=0
   t=4005761             tick      Damaged 12 by occupy, Died   (event tick 4005721)
   terrain (397..403, 528): g g f d l l f   ← (401, 528) lava, occupy_damage 12
   ```

   Suspects: `runner.py:704` builds directional Steps from `w.pos`, so a target cell becomes a wrong direction once the running queue's own step has moved the character. Flee's hazard filter (`states/explore.py:179`, `blocked` includes `avoid_blocks`) cannot catch it: it checked (400, 529), not where the Step lands. Re-read position before replacing a queue whose Step may have run, or skip the replacement when the queued first step equals the new target.

2. **A hostile that shadows at 5 blocks stops all cutting.** From 104 s npc 269 (wartlurch) followed at Chebyshev 4–6 for 40 s without attacking. `gather_ground` rules out any cell within `GATHER_HOSTILE_RADIUS` (6) of a hostile, so the grass around the character was never cuttable. Gather neither cut nor shook it off: it replanned 1–5 steps at a time to the nearest grass outside the radius, which the follower then covered again, and walked 55 blocks north into a hostile cluster (npcs 262, 263, 264) and a lava field. Gems stayed at 6 from 71 s to 149 s; 0 cuts after 104 s.

   ```
   t=4005135 @76:404,586 tick  gather → (403, 585)     npc 269 at 5
   t=4005295 @76:394,571 tick  gather → (393, 570)     npc 269 at 5
   t=4005442 @76:384,557 tick  gather → (383, 556)     npc 269 at 5
   gather_run: cuts 28 at 107 s, 28 at 152 s
   ```

   Suspects: `states/gather_safe.py:41` with `:22` (the path planner's danger radius used as a hard bar), and `states/gather.py:255–259` (grass replan picks the nearest qualifying cell, so it inches away step by step). A hostile that has not hit us could be fought (the M8 profile has `on_hostile = "fight"`), or the bar could be weapon reach plus a step, not 6.

3. **The planner calls the stall "working".** During the 45 s with no cut, `gather_status` stayed `"cutting"` (a walk to a grass cell counts as cutting, `states/gather.py:121–128`), and three planner calls read it as progress while `gather_run` showed cuts flat at 28:

   ```
   call 8   "A gather_gems pin to 20 gems is on top and working: 6 gems from 28 cuts"
   call 9   "Pinned gather_gems to 20 is working: 6 gems from 28 cuts in the best region"
   call 10  "Pinned gather_gems to 20 stays on top; gems are 6 and the yield is about 0.27 per cut."
   ```

   Suspects: `states/gather.py:128` (`CUTTING` for any walk) and the State, which has no reason for a walk (e.g. "keeping 6 from npc 269") and no time since the last cut. With `gem_yield.best` empty past 3 regions (`gem_yield.py:61`), the planner also lost the one productive region it could have sent the character back to.

**Minor:** the authored pile cluster at (386–388, 535–536) and the lone gem at (400, 529) sat among hostiles and lava; Gather walks to any pile in view first (`states/gather.py:234`), checked against hostiles only at plan time, so the queue to (400, 529) ran on while npc 264 closed to 5 blocks.
