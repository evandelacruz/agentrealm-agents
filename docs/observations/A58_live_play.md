# A58 live M7 hour (redacted): not a PASS yet

Live runs against agentrealm.gg with `scripts/smoke_m7_olympuff.py` and the A16 gate. Credentials and character ids are omitted.

## Account / character

- **Character cap:** `smoke-character-b` could not be created (`character_cap_reached`). The run reused **`smoke-character-a`** via a local TOML copy (not committed). Free a slot or delete an unused Olympuff observer before expecting the dedicated M7 character from `python/characters/olympuff_m7.toml`.
- **Lives:** 8 at the end of the long run (one death in an earlier 20-minute diagnostic).

## Run 1 — full wall clock (~3600 s)

- **Gate:** navigation **reached** the 150-block east target; 0 deaths in this run; 0 API errors; no loop.
- **Fail:** `safe-zone regen never measured` (`heal actions: 0`). The agent crossed the hour mostly at full health, and only a hurt character can show regen, so Heal never ran its probe.
- **Log:** kept outside the repo, not committed.

## Run 2 — blocked at start

A second full-hour attempt failed immediately: `start: HTTP 409 not_on_map`. The reused character was **asleep** (`placed: false`: auto-sleep after 10 idle minutes), so it had no position. That was a smoke-script bug, not a game blocker: `navigation_start()` read the position before sending any intent, and any intent wakes a sleeping character. The script now reads self first, sends one `Wait` while asleep and re-reads until it is awake, waits out a respawn while downed, and exits with a clear message on `alive_cap_full` or `block_occupied`.

## Agent changes this pass (for the next live hour)

- Defer Loot, Shop, Investigate, Travel, OddBreak and non-`goto` Break while `goto_navigation_pending`; skip the plan's ops and `wait` hold.
- Let policy `goto` outrank plan travel during the 150-block walk; keep an in-flight goto path instead of replan ping-pong.
- Heal is not deferred: a hurt character still walks to safety mid-goto.
- The goto path is kept through Explore's normal flow, so stuck detection still escalates and gives up on it.
- The pacing seen in run 3 is fixed by the dispatch oscillation guard, not by more goto deferrals (see Run 3 below).
- A full-health regen probe was tried and dropped: health cannot rise at full health, so it could only ever report "no". Regen is measured only while hurt; the gate's regen check still needs a run that is hurt in a safe zone.

## Run 3 — aborted: pacing between two cells

- **Character:** chosen at run time, as in run 1 (not committed).
- **Gate focus:** safe-zone regen must get a yes/no verdict during a hurt window in a safe tile; navigation already passed on run 1.
- **Run 3a (stopped after ~5 min):** during the goto walk the character paced back and forth between two cells around x≈616–617 while the goto's stuck escalation was at step 2 (Break). Stopped by hand, so the gate never ran: no verdict, regen not measured.
- **Run 3b (stopped after ~3 min):** the same pacing, now with an Equip `Wear` and Break walks taking turns with the goto walk. Stopped by hand, so the gate never ran: no verdict, regen not measured.
- **What was wrong:** each fix after 3a patched one more state (relabel Break's walk as `goto`, skip replan, defer Equip, hold Break while a `Use` was pending). That was the bug: any two states that take turns moving the character can pace, so a fix per state never ends. Those patches are removed again.
- **Fix:** one oscillation guard in dispatch (A15, `navigation/oscillation.py`). When the character's last 6 cell changes stayed on at most 2 cells, it gives up the target it walks to through stuck detection's step 5 (reason `pacing`) and traces an `oscillation` event, whichever states caused it. The smoke script now aborts with exit 1 on sustained oscillation (more than 3 give-ups in 6000 ticks; events where survival states did the moving do not count), so a live run cannot burn the hour pacing.
- **Status:** tested offline only. **Next:** rerun the full live hour.

## Run 4 — stopped (~10 min): Flee ping-pong between two cells

- **Character:** chosen at run time via `CHARACTER_NAME` (not committed).
- **Gate:** navigation gave up on the 150-block `goto` with reason `pacing` between two cells near the start (~613–614 on x); regen never measured; 0 deaths; smoke did not abort (Flee pacing has no `goal` on oscillation events).
- **Trace:** from roughly tick 3428289 the agent paced between two diagonal neighbours (`614,398` ↔ `615,397`) under **Flee**, alternating `flee npc 140` and `flee npc 142`. No Heal, no regen verdict, no progress toward explore or the goto target for ~10 minutes; run stopped by hand.
- **Cause:** with two hostiles in range, greedy `flee_step` maximised distance to the nearest NPC each tick, so the best step from each cell was back to the other.
- **Fix:** see *Agent changes this pass (after runs 4–6)* below. A first patch that skipped the step back to the cell just left was withdrawn.

## Run 5 — ABORT: sustained oscillation on goto (~5.5 min)

- **Character:** chosen at run time via `CHARACTER_NAME` (not committed).
- **Gate:** exit 1 `ABORT: sustained oscillation` — 4 goto pacing give-ups in 6000 ticks (last at 425↔426 on y=375); regen not measured; 0 deaths; ~333 s wall clock.
- **Trace:** 24 oscillation events (4 with `goal: goto`); one Loot-only pacing event (`385,371`↔`385,373`, nothing given up). No Flee ping-pong after the Run 4 fix.
- **Cause:** Explore replanned the goto path each tick and kept stepping back to the cell just left (393↔394, then 425↔426), tripping the oscillation guard repeatedly after each backoff ended.
- **Fix:** none beyond the A15 guard, which did its job here: each pacing spot was given up and the smoke run stopped instead of burning the hour. A back-step block on the goto walk was tried and withdrawn: it also blocked a legitimate single step back toward a target behind the agent. Why the goto replan flipped at those two spots is not known from this trace.

## Run 6 — stopped (~12 min): Flee still pinned (live run budget exhausted)

- **Character:** chosen at run time via `CHARACTER_NAME` (not committed).
- **Gate:** not finished; 0 deaths; regen not measured; stopped by hand after ~12 minutes on the same two cells (`614,397` ↔ `615,396`) with **Flee** and goto **Break** queues alternating.
- **Trace:** one `oscillation` with `goal: goto` early; survival pacing did not abort the smoke run. Flee anti-backstep (Run 4 fix) was too weak when only the reverse step scored best; Break escalation for the goto still walked while hostiles were in range.
- **Fix:** see below. Deferring goto **Break** while `should_flee` was tried and withdrawn: Flee outranks Break and always sends an intent or waits, so Break never ran while `should_flee` held; the alternation came from Flee pacing, which the committed escape removes.

## Agent changes this pass (after runs 4–6)

- **Flee commits to an escape (A9).** Re-picking the greedy best step every decision cannot settle against two moving hostiles: each of their moves makes the cell just left the best again. Flee now plans a multi-step escape and walks it until it arrives, is blocked, or Flee stops. The first step is still the best single step (ties toward a known safe tile; standing still when cornered); the rest is a route to the nearest known safe tile when the agent gets to every cell of it before any hostile could, else up to 6 seen cells away from the hostiles under the same rule. Offline test `test_two_moving_hostiles_do_not_pin_flee_between_two_cells` rebuilds the runs 4/6 pin (two NPCs stepping back and forth, the agent on a diagonal pair); it fails on `main` and passes here.
- **Survival pacing corrects itself in the guard (A15).** When only Flee and Retreat made the pacing moves, the oscillation guard hands the paced cells to them: on that same decision Flee plans a fresh escape that keeps off them, or Retreat routes around them. Nothing is given up and the smoke abort count is unchanged.
- **Withdrawn:** the reverse-step skip in `flee_step`, the goto back-step block (`goto_back_avoid`, `nav_blocked_for_walk`), and the Break deferral while `should_flee`. `test_the_goto_walk_pacing_on_its_own_is_given_up` shows the guard alone ends run 5's goto pacing; `test_a_goto_behind_the_agent_takes_the_step_back` keeps a single step back allowed.
- **Status:** merged (#92, #93); the next live hour was run 7, below.

## Run 7 — FAIL: full hour, goto dropped short and Heal paced (~3600 s)

- **Character:** chosen at run time via `CHARACTER_NAME` (not committed).
- **Verdict:** exit 1 after **3600.5 s** wall clock. **Did not pass** the A16 gate.
- **Gate metrics:** deaths 0; retreat misses 0; recover withdraws 0; loop false; API errors 0; oscillation events 417 (gave up a target: 0); heal actions 2526; lives last seen 5; navigation target `(575, 375)` from `(425, 375)` **neither reached nor given up** (max Chebyshev from origin 237); safe-zone regen **not measured**.
- **Goto (first ~13 min):** the agent walked east to `(556, 369)` (~19 blocks short of the smoke target) with queues labelled `goto → (556, 369)` and intermediate corridor waypoints. At tick **3475822** it stood on `(556, 369)` with the last goto queue still aimed at that waypoint; at **3475832** the next queue was **`explore → (525, 385)`** with no stuck give-up on `(575, 375)`. The hour then wandered south and west; Flee fired briefly (committed escape, no run 4/6-style pin) but never returned to finish the 150-block walk.
- **Heal / regen (most of the hour):** from tick **3479055** onward **Heal** walked **`heal_food → (400, 611)`** with **2526** heal actions. From **3481152** through **3511049** the character paced between **`(404, 607)`** and **`(404, 608)`** (417 oscillation events, all `nothing given up, moved by Heal`). Example window:

  ```
  t=3482195 oscillation pacing [[404, 606], [404, 607]]: nothing given up, moved by Heal
  t=3482196 queue 1×Step 2×Wait (heal_food → (400, 611))
  t=3482201 @404,607 — (queue held)
  t=3482212 @404,608 — (queue held)
  t=3482264 oscillation pacing [[404, 607], [404, 608]]: nothing given up, moved by Heal
  t=3482265 queue 1×Step 2×Wait (heal_food → (400, 611))
  … same two cells and 1–2 step queues for the rest of the hour …
  t=3511036 oscillation pacing [[404, 607], [404, 608]]: nothing given up, moved by Heal
  ```

- **Root cause (from the code; no agent change in this PR):**
  1. **Navigation (A16 bug).** `(556, 369)` was the end of the goto's planned path (Explore labels a step with `m.path[-1]`), not the target. Arriving there, Explore's kept path has no next step, so it calls `pathing.replan`. `replan` tries `goto` first; when the goto plan has no seen, open first step, it falls through to `explore`, which gets the step. The goto's miss is handed to stuck detection only when no goal gets a step, so the goto attempt never failed a window, never escalated and was never given up, while `goto_navigation_pending` stayed true. Every later decision repeated that fallthrough. Why the goto plan found no open step from `(556, 369)` is not in this trace.
  2. **Regen (A10 bug).** `HealState._walk_toward` keeps `m.path` and replans only when the goal or endpoint changes or the next step is blocked; it does not replan every tick. The gap is that nothing bounds the food walk. Each `heal_food` decision sends a `Step`, and `HealState.act` clears `heal_wait` whenever it sends intents, so A10's "no health back for 600 ticks, yield to Explore" never starts. The oscillation guard gives nothing up for Heal's own moves, as A15 specifies, so neither it nor the smoke abort stopped the walk. The run spent ~30k ticks on one food item at `(400, 611)`, pacing `(404, 607)` ↔ `(404, 608)`, never resting on a safe tile for `note_regen_sample`.
- **Why the two cells.** The window search the planner falls back on when its node budget runs out never counted the cell the agent stood on. Beside food it could not reach, it always stepped to a neighbour, and from that neighbour the start was the best cell again. The same holds at the goto's last waypoint: there the target, once in sight, had no path at all.
- **Fix (#101, tested offline):**
  - `replan` tries only the `goto` while it is owed, so a goto with no step is escalated and given up by stuck detection on that target (A16).
  - The window search lets the agent stay put; a cell no better than where it stands is no path.
  - Every Heal walk, and Loot's walk to a pickup, is a stuck attempt (`pathing.bounded_step`), given up on no path, on no progress in 20 moves or 300 ticks, or by the oscillation guard (A10, A15).
  - Regression tests in `python/tests/test_a58_run7.py`: `test_goto_stays_owed_until_stuck_detection_gives_it_up`, `test_explore_moves_only_while_the_goto_is_backed_off`, `test_heal_gives_the_food_up_instead_of_pacing`, `test_loot_gives_the_pickup_up_instead_of_pacing`, `test_the_planner_has_no_step_off_the_dead_end`, `test_heal_only_pacing_is_given_up_by_the_guard`, and the `bounded_step` window tests; plus `test_an_owed_goto_starting_in_fog_keeps_the_move` in `test_cost_grid.py`.
- **Status:** **Next:** rerun the full live hour.

## Run 8 — FAIL: ABORT sustained oscillation after goto reached (~1709 s)

- **Character:** chosen at run time via `CHARACTER_ID` (not committed).
- **Verdict:** exit 1 after **1708.6 s** wall clock (`ABORT: sustained oscillation`). **Did not pass** the A16 gate (run stopped before the hour).
- **Gate metrics:** deaths 0; retreat misses 0; recover withdraws 0; loop false; API errors 0; heal actions 0; lives last seen 10; navigation target `(568, 490)` from `(418, 490)` **reached** (max Chebyshev from origin 160); give-ups on other goals 5; safe-zone regen **not measured**; oscillation events 5 (gave up a target: 5).
- **Goto:** the agent completed the 150-block east walk and stood on `(568, 490)` (smoke target) before tick **3531805**, the first give-up, which was already pacing off the target; the exact reach tick is not in the saved trace excerpt. The whole run was 1709 s, and about 16,000 ticks of it (3531805 to 3547802) were pacing after the reach. Explore then picked frontiers south/west of that cell.
- **Pacing (after the reach):** **Explore** walks toward frontiers alternated with **goto** walks back toward `(568, 490)` whenever the character stepped off the target (A16 goto-first comes back). The first oscillation give-up was a `look` frontier at **3531805**; then four `explore_area` give-ups at **3545075**, **3545761**, **3547112** and **3547802**, the fourth of which (four in 6000 ticks) fired the smoke abort. Example window (final abort):

  ```
  t=3547708 @558,485 queue 10×Step 28×Wait (goto → (568, 490))
  t=3547748 @568,490 — (queue held)
  t=3547753 @568,490 queue 10×Step 28×Wait (explore_area → (541, 499))
  t=3547792 @558,485 — (queue held)
  t=3547802 @558,485 oscillation pacing [[558, 485], [568, 490]]: gave up explore_area → (541, 499)
  t=3547803 @558,485 queue 10×Step 29×Wait (goto → (568, 490))
  ABORT: sustained oscillation … (last at tick 3547802 … moved by Explore)
  ```

  The `look` give-up at **3531805** paced `(568, 490)` ↔ `(578, 482)`; the explore give-ups at **3545075**, **3545761** and **3547112** paced `(568, 490)` ↔ `(569, 480)` for `explore_area → (581, 463)`.

- **Root cause: a spec defect, not an implementation bug.** A16's goto-first rule says the deferral comes back when the agent steps off the target, and `goto_navigation_pending` in `pathing.py` does exactly that: once the agent leaves the reached target, the goto is owed again, so `replan` tries only the goto while Explore owns its frontier walk. The two queues ping-pong between the target and the frontier; the oscillation guard correctly gives up each explore target (`navigation/oscillation.py`), and the smoke abort fires after the fourth give-up in 6000 ticks. **Proposal (PLAN.md A16):** proposed: a reached policy goto is satisfied and not re-owed on step-off; awaiting Evan.
- **Regen never measured.** The character was never hurt (`heal actions: 0`), so there was no hurt safe-zone window for `note_regen_sample`. Even a full hour may need the character to take damage before regen gets a verdict.
- **Status:** Two open blockers. (1) Navigation pacing: the A16 goto-first change is proposed: a reached policy goto is satisfied and not re-owed on step-off; awaiting Evan; once decided, implement it with a regression test. (2) Regen: unmeasured on every run so far (1 and 3–8); in run 8 the character was never hurt, so even with the A16 fix, run 9 can fail the regen gate. **Next:** after both are addressed, rerun the full live hour (A58 run 9).

## Done-when

A58 stays open until a live hour exits 0 on the A16 gate and a redacted PASS transcript is committed under `docs/acceptance/m7_olympuff_PASS.transcript`.
