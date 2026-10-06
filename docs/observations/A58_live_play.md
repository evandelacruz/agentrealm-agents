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
- **Status:** offline only; no live run since run 6. **Next:** the full live hour once this merges.

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

- **Root-cause hypothesis (fixer, no code in this PR):**
  1. **Navigation:** Explore took the walk while the smoke `goto` to `(575, 375)` was still owed, after the two-level planner’s last corridor waypoint `(556, 369)` without a give-up on the ultimate target — likely **`pathing.replan` / Explore** treating a partial corridor leg as done (see tick window above).
  2. **Regen:** **`HealState._walk_toward`** in `states/heal.py` replans a one- or two-step cost path to ground food each tick; on this terrain the best steps alternated across `(404, 607)` and `(404, 608)`, so Heal never reached `(400, 611)` or rested on a known safe tile for **`note_regen_sample`**. The oscillation guard correctly gives survival Heal moves **nothing** to give up, so the smoke script did not abort but the hour burned on pacing.

## Done-when

A58 stays open until a live hour exits 0 on the A16 gate and a redacted PASS transcript is committed under `docs/acceptance/m7_olympuff_PASS.transcript`.
