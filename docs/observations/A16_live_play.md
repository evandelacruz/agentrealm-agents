# A16 live probe (redacted): not a PASS

An hour on `olympuff` through the public API with an earlier version of the M7 smoke script. It exited 0, but that gate was too loose: it passed navigation on any give-up and never required regen. Against the A16 gate as it stands now (PLAN.md A16, README **Tests**), this run **fails**. The live hour that passes is A58.

## Run

- **Character:** `OlympuffM6Walk5d4e`, a reused M6-era slot, because the account was at its character cap. Character id omitted. The acceptance config now uses its own character, `OlympuffSurvivor`.
- **Policy:** scripted `explore`, `on_hostile = flee`, `pickup = false`. Pickup had been switched off with no recorded reason; it is back on, since Recover (A11) needs it.
- **Navigation:** explore only. There was no `goto` target, so nothing could reach or give up on a point 150 blocks away.

## Metrics (final summary)

| Metric | Value |
|---|---|
| Wall clock | 3600.1 s |
| Max chebyshev distance from overworld origin | 126 |
| Deaths | 0 |
| Retreat misses (counted after the tick, an old bug) | 0 |
| Recover withdraws (unsafe) | 0 |
| Heal actions | 65 |
| Safe-zone regen measured | no answer |
| Loop detected | no |
| API errors | 0 |
| Lives | 10 throughout |

## Against the current gate

- **Navigation fails.** 126 of 150 blocks, and the give-ups that passed it were on explore frontier cells, not on a 150-block target.
- **Regen fails.** No yes or no answer was recorded.
- Survival held: no deaths, no API errors, no loop.

## Notes

- Shop steps appeared late in the hour when the agent routed toward priced potions; no purchase is needed for M7 (that is M8).
