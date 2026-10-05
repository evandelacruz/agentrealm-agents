# A58 live M7 hour (redacted): not a PASS yet

Live runs against agentrealm.gg with `scripts/smoke_m7_olympuff.py` and the A16 gate. Credentials and character ids are omitted.

## Account / character

- **Character cap:** `OlympuffSurvivor` could not be created (`character_cap_reached`). The run reused **`OlympuffM6Walk5d4e`** via a local TOML copy (not committed). Free a slot or delete an unused Olympuff observer before expecting the dedicated M7 character from `python/characters/olympuff_m7.toml`.
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
- A full-health regen probe was tried and dropped: health cannot rise at full health, so it could only ever report "no". Regen is measured only while hurt; the gate's regen check still needs a run that is hurt in a safe zone.

## Done-when

A58 stays open until a live hour exits 0 on the A16 gate and a redacted PASS transcript is committed under `docs/acceptance/m7_olympuff_PASS.transcript`.
