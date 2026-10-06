# A16 live Walk runs with the planner on (redacted)

The 5-minute Walk: `scripts/smoke_m7_olympuff.py --seconds 300` with the AI planner on, the pinned 150-block east target on top of the stack, survival judged only. Runs 1 and 2 are written up in #113 and #115. Character chosen at run time; no name or id is committed.

## Run 3 — FAIL: death at 132 s, pinned target never approached

- **Code:** `main` at `e2f9d8a` (merge of #115). Planner: Anthropic, default model.
- **Verdict:** exit 1 after **132 s** (first death ends the run). Character started at 6/10 health, 10 lives, 1 gem.
- **Gate summary:** deaths **1**; API errors **1** (`position 409 not_on_map`, the read that raced the death); navigation target (+150, 0) **neither** reached nor given up (max Chebyshev from origin **30**); give-ups 0; returns to a given-up target 0; oscillation events 0; heal actions 0; retreat misses 0; regen not measured.

### Planner

| | |
|---|---|
| Calls | 10 (2 `applied` + 1 `unchanged` early, 6 `kept` with no goals, 1 `applied` on the hurt trigger) |
| Plans accepted / errors | **4 / 0** |
| Tokens | input 5,679, output 1,308, cache write 97,780 (call 1), cache read 880,020 |
| Tokens per minute (budgeted) | **~98k in minute 1** (the one cache write), then **~2.5–3.5k/min** |

The planner's own part of the stack was sensible from call 2: `explore_area`, `gather_gems` ×15, `buy bronze_sword`, `buy small_potion`, `equip`. On the hurt trigger it moved `gather_gems` to the top of its part. None of it ever ran: the pinned `travel:point` stayed on top for the whole run.

### Decision mix (50 decisions that sent intents; 87 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| travel (pinned `travel:point`, incl. legs) | 25 |
| flee | 15 |
| break (Travel's escalation step 2: walk to and cut grass) | 4 |
| path stale, resend | 1 |
| explore, gather_gems, buy, equip, heal | 0 |

Call mix over the run: 167 `zone`, 132 `tick`, 17 `position`, 11 `terrain`, 10 `self`, 5 `entities`.

### Outcomes

- **Gems:** 1 → 1 (none gathered).
- **Deaths:** 1 (lives 10 → 9).
- **Stalls > 10 s at one cell:** none. But the walk made **no net progress for 130 s**: x stayed in 591–628 against a target at +150.
- **#115 (given-up targets come back):** not exercised. Stuck detection never gave up, so `given_up_travel` stayed empty; returns 0.
- **Pinned ops yield to gather_gems:** **no.** The pinned travel held the top of the stack for the entire run. The planner placed `gather_gems` first in its own part (call 10), but nothing below a pinned op runs until it is done or given up.

### Top 3 defects

1. **Travel's escalation loops WALK → CAUTIOUS → BREAK → WALK and never reaches REVEAL, ALT_ROUTE or give-up.** Each cut grass resets the attempt to WALK with a fresh window, so stuck detection can never give up the pinned target. It blocks the stack for good (no gather_gems, no #115 drop).

   ```
   t=3933405 @76:615,424 tick  queue … (travel:point → (628, 427) [cautious])
   t=3933578 @76:618,407 tick  queue … (break → (623, 412))
   t=3933600 @76:622,411 tick  Use(block:) (break cut @ (623, 412))
   t=3933643 @76:628,406 tick  queue … (travel:point → (755, 413))          ← back to WALK
   t=3934093 @76:623,392 tick  queue … (travel:point → (755, 413) [cautious])
   t=3933918 @76:618,408 tick  queue … (break → (626, 413))
   t=3933991 @76:627,412 tick  queue … (travel:point → (755, 413))          ← WALK again
   ```

   Suspect: `navigation/stuck.py:274` `on_break_opened` sets `att.level = WALK` and `_fresh_window` (called from `runner.py:1107`). A break that only cuts grass beside a route that then fails the same way should not reset the escalation ladder.

2. **The pinned travel walks a hurt character back into a known hostile group, and Flee cannot get clear.** Starting at 6/10, the walk headed north-west around the obstacle, fled npc 228 near (617, 387), then re-routed north again to (605, 383), next to npc 226 at (606, 382). Two hits and death followed within 6 s.

   ```
   t=3933729 @76:617,387 tick  Step(down_right) (flee npc 228)
   t=3934405 @76:601,387 tick  queue 7×Step 19×Wait (travel:point → (755, 413) [cautious])
   t=3934420 @76:605,383 tick  queue 10×Step 27×Wait (path stale, resend) | Attacked, Damaged 2 by npc
   t=3934442 @76:606,385 tick  Step(down_right) (flee npc 226) | Attacked, Damaged 2 by npc
   t=3934475 @? tick     — (queue held) | Attacked, Damaged 2 by npc, Died
   ```

   Suspects:
   - `navigation/walk.py:133–134`: a found path whose first step turns back (`back`) always loses to the kept path, whatever the kept path's hostile cost.
   - `strategist.py:148` and `plan.py` pin semantics: the planner sees the character at 4/10 with no potion and still cannot demote the pinned walk, so the walk keeps priority over survival.
   - `states/flee.py`: Flee lost a 1-on-1 chase again (as in A58 run 9).

   The planner also tried `retreat_hits: 1` "to tighten survival", which `plan.py:449` rejected as below the floor. The rejection is correct; the planner misread the param's direction.

3. **A spurious `idle` trigger fires 4 s into the run.** `runner.py:270` seeds `strategist_progress_tick` from `world.tick` before any tick is known (0). The first window with the real tick then satisfies `strategist.py:706–710` at once. Trace: call 2 `{"trigger": "idle", "since_tick": 0, "tick": 3933174, "idle_ticks": 6000}`. The planner wrote "idle for 6000 ticks" into its goals. It costs one call and misleads the plan.

**Minor:** the one API error is a `position` read sent in the same window the character died, before the `Died` event arrived (`t=3934459 position error HTTP 409 not_on_map`). This is the A5 pattern from A58 run 9, now down to one call.

## Run 4 — FAIL: death at 75 s, before any of the #119 fixes could be exercised

- **Code:** `main` at `7f9c795` (after #119, the Walk run 3 fixes, and #120). Planner: Anthropic, default model. Fresh knowledge base (no `.state`).
- **Verdict:** exit 1 after **75 s** (first death ends the run). Character started at 10/10 health, 10 lives, 0 gems, pocket knife armed, about 80 blocks west of the town cell.
- **Gate summary:** deaths **1**; API errors **0**; navigation target (+150, 0) **neither** reached nor given up (max Chebyshev from origin **96**); give-ups on other goals 3 (all `heal_food`); returns to a given-up target 0; oscillation events 0; heal actions 1; retreat misses 0; regen not measured.

### The four checks

| Check | Result |
|---|---|
| Break loop escalates to give-up | **Not exercised.** The walk never reached BREAK: escalation got as far as `[cautious]` (from t=3954796), then the character died. |
| Hurt walk avoids hostiles | **No, in practice.** After the first hit (7/10) the pinned walk resumed and, at 8/10, led straight into a group of three hostiles (gristlewick 217, snotlings 215/216 at (401–404, 353–361)). None of the three appears in an entity read before the first hit, so the #119 step-back rule (`navigation/walk.py:135–144`, known hostiles only) had nothing to act on. See defect 2. |
| Spurious `idle` trigger | **Gone.** Call 2 fired on `timer` at t=3954448; no `idle` trigger in 6 calls. The 75 s run is shorter than a real idle window, so only the false start is ruled out. |
| Planner ops run once the target is given up | **Not exercised.** The pinned target was never given up or reached, so `gather_gems`, `explore_area` and `buy` stayed below it and never ran. |

### Planner

| | |
|---|---|
| Calls | 6 (4 `applied`, 2 `unchanged`) |
| Plans accepted / errors | **6 / 0** |
| Tokens | input 3,511, output 1,380, cache write 98,054 (call 1), cache read 490,270 |
| Tokens per minute (budgeted) | **~98.6k in minute 1** (the one cache write), then ~4.4k |

The planner's part of the stack was sensible: `explore_area` near the start, then `gather_gems` ×15, `buy bronze_sword`, `buy small_potion`. On the three `heal_food` stuck triggers it noticed that bushes block the berries and the pocket knife cuts bushes, and moved `gather_gems` to the top of its part. On the hurt trigger (4/10) it tightened `fight_margin` 2.0 and `risk` 0.3, and again tried `retreat_hits: 1` "to retreat sooner", which `plan.py:456` rejected as below the floor (the run 3 misreading, unchanged).

### Decision mix (39 decisions that sent intents; 43 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| flee (npc 221, then npc 217) | 20 |
| travel (pinned `travel:point`, legs) | 13 |
| `not outrunning npc 217: fight npc 217` (Flee falling back to a swing) | 5 |
| heal_food | 1 |
| break, explore, gather_gems, buy, equip | 0 |

Intents: 140 `Step`, 321 `Wait`, 5 `Use`. Call mix: 82 `tick`, 76 `zone`, 24 `position`, 12 `strategist`, 11 `entities`, 9 `terrain`, 6 `self`.

### Health

10 → 8 → 7 (npc 221, t=3954429–3954447) → 8 (berry at (390, 350), t=3954733) → 6 → 4 → 2 → 1 → dead (gristlewick 217, t=3954911–3955034). From the first hit by 217 to death took **12 s**. All 76 zone reads came back `safe=False`, so no safe tile was known for Retreat.

### Top 3 defects

1. **Flee loses the chase to a gristlewick, then swings back below the health floor until it dies.** With no safe tile known, `instead_of_fleeing` falls through to "swing back at the hitter" whatever the win estimate says. Swings went out at 4/10 (twice), 2/10 (twice) and 1/10: at or below a floor of 4 (`retreat_hits` 2 × a hit of 2). The pocket knife landed one hit (`NPCDamaged` once).

   ```
   t=3954913 @76:402,361 tick  Step(down_right) (flee npc 217) | Attacked, Damaged 2 by npc
   t=3954931 @76:404,361 tick  Step(right) (flee npc 217) | Attacked, Damaged 2 by npc
   t=3954940 @76:405,361 tick  Use(npc:217) (not outrunning npc 217: fight npc 217)
   t=3954946 @76:405,361 tick  Use(npc:217) (not outrunning npc 217: fight npc 217) | NPCAttacked, Attacked, Damaged 2 by npc
   t=3955006 @76:410,366 tick  Step(up_right) (flee npc 217) | Attacked, Damaged 1 by npc
   t=3955021 @76:411,365 tick  Use(npc:217) (not outrunning npc 217: fight npc 217) | Attacked
   t=3955036 @? tick           Step(down_right) (flee npc 217) | Attacked, Damaged 2 by npc, Died
   ```

   Suspects: `states/flee.py:103–104` adds the swing-back option with no `would_lose` or floor check, so a losing fight is chosen once running and retreating both fail; `states/retreat.py:53–54` returns no intent when no safe tile is known, so the walk never heads for the town the world read named.

2. **Entity reads stop while the walk is moving, so it walks into hostiles it has not read.** 11 entity reads in 75 s, none between t=3954454 and t=3954915 (46 s), while 76 zone reads went out in the same run. The tick deltas fold entities in and reset `entities_tick`, so the 20-tick `entity_refresh` never comes due. The three hostiles that killed the character show up in an entity read only one tick after the first hit, and the cautious walk's queue sent at t=3954900 had its first step land beside 217.

   ```
   t=3954454 @76:342,383 entities npc:221@340,385, …                     ← last read before the walk
   t=3954900 @76:400,359 tick  queue 10×Step 27×Wait (travel:point → (417, 352) [cautious])
   t=3954904 @76:401,360 tick  Step(down_right) (flee npc 217)
   t=3954913 @76:402,361 tick  Step(down_right) (flee npc 217) | Attacked, Damaged 2 by npc
   t=3954915 @76:402,361 entities npc:216@401,353, npc:215@401,354, npc:217@401,360, …
   ```

   Suspects: `world.py:482–483` (`_apply_delta_body` sets `entities_tick` on any entity delta) and `brain.py:82` (the refresh rule reads it). If deltas only carry nearby entities, a walk heading into new ground needs a real read on a fixed cadence.

3. **Heal gives up berries behind bushes it could cut.** Three `heal_food` targets were given up as `no_path` with `blocking: ["bush"]`, though the pocket knife cuts bushes (the planner pointed this out in call 5). The character went into the fatal encounter at 8/10 instead of 10/10.

   ```
   call 5 triggers: stuck heal_food:76:393,337 no_path blocking [bush]; heal_food:76:389,340 …; heal_food:76:389,339 …
   ```

   Suspects: `states/heal.py:204` walks only; `pathing.py:619` gives up `no_path` without asking Break to open the blocker.

**Minor:** the planner's hurt reply changed `fight_margin` and `risk` but the log line read `same stack; progress kept`, because the stack itself was unchanged. Params changed silently.
