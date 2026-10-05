# A16 live play (redacted)

M7 acceptance on `olympuff` via the public API. Character name `OlympuffM6Walk5d4e` (existing Olympuff slot; account character cap blocked a new create). Character id omitted.

## Run

- **Command:** `python3 scripts/smoke_m7_olympuff.py` (3600 s wall clock, default).
- **Policy:** `python/characters/olympuff_m7.toml` — scripted `explore`, `on_hostile = flee`, `pickup = false`.
- **Outcome:** PASS (smoke exit 0).

## Metrics (final summary)

| Metric | Value |
|---|---|
| Wall clock | 3600.1 s |
| Max chebyshev distance from overworld origin | 126 (target 150 or documented give-up) |
| Deaths | 0 |
| Retreat misses | 0 |
| Recover withdraws (unsafe) | 0 |
| Heal actions (food / path fallbacks) | 65 |
| Safe-zone regen measured | no (not saved this run) |
| Loop detected | no |
| API errors | 0 |
| Lives | 10 throughout |

## Notes

- Navigation satisfied the smoke check via stuck-detection give-up signals recorded during the hour while explore pushed outward from the town pocket (max distance 126 before escalation/backoff).
- Shop steps appeared late in the hour when the agent routed toward priced potions; no purchase required for M7 (M8).
- Prior probe runs on the same character exercised Heal heavily while hurt; this PASS run stayed mostly on explore/flee with modest Heal use.
- Full console log: `/opt/cursor/artifacts/m7_smoke_final.log` on the acceptance runner (not committed; transcript-sized).
