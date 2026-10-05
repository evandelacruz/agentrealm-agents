# A58 live M7 hour (redacted): not a PASS yet

Live runs against agentrealm.gg with `scripts/smoke_m7_olympuff.py` and the A16 gate. Credentials and character ids are omitted.

## Account / character

- **Character cap:** `OlympuffSurvivor` could not be created (`character_cap_reached`). The run reused **`OlympuffM6Walk5d4e`** via a local TOML copy (`/opt/cursor/artifacts/olympuff_m7_run.toml` in the agent environment). Free a slot or delete an unused Olympuff observer before expecting the dedicated M7 character from `python/characters/olympuff_m7.toml`.
- **Lives:** 8 at the end of the long run (one death in an earlier 20-minute diagnostic).

## Run 1 — full wall clock (~3600 s)

- **Gate:** navigation **reached** the 150-block east target; 0 deaths in this run; 0 API errors; no loop.
- **Fail:** `safe-zone regen never measured` (`heal actions: 0`). The agent crossed the hour mostly at full health and never stood in a known-safe cell long enough for Heal’s regen probe.
- **Artifacts:** `/opt/cursor/artifacts/m7_smoke_full.log` on the cloud agent (not committed; redact if copied).

## Run 2 — blocked at start

After code changes to probe regen at full health, a second full-hour attempt failed immediately: `start: HTTP 409 not_on_map` — the reused character was **`placed: false`** (off the map / asleep). Re-run once the slot is back on the overworld.

## Agent changes this pass (for the next live hour)

- Defer Loot, Shop, Investigate, Travel, and non-`goto` Break while `goto_navigation_pending`.
- Let policy `goto` outrank plan travel during the 150-block walk; keep an in-flight goto path instead of replan ping-pong.
- Heal may measure safe-zone regen at full health when regen is still unknown (without preempting Recover on a death chest).

## Done-when

A58 stays open until a live hour exits 0 on the A16 gate and a redacted PASS transcript is committed under `docs/acceptance/m7_olympuff_PASS.transcript`.
