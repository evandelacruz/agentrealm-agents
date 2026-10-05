# A30 live play (scroll codes)

Attempted on 2026-10-05 against the public Olympuff API to learn which
`supply_subtype_code` values answer `Read {kind: supply}` (GAME_NOTES open
questions).

## Outcome

No scroll subtype codes were observed. Live play could not run the
GAME_NOTES test (log every supply seen, `Read` one of each code, keep those
that are not `nothing_to_read`).

## Blockers

- **New character:** `POST /worlds/olympuff/characters` returned
  `character_cap_reached`.
- **Existing characters:** Every listed Olympuff character except
  `OlympuffWalker` returned `not_on_map` on `GET …/position` (and could not
  be walked). `OlympuffWalker` returned `rate_limited` on repeated reads.

Until a dedicated character can tick on-map, or observation notes a scroll
code, **Investigate** still does not nominate scroll reads (PLAN.md A54).
