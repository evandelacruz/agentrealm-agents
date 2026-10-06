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
