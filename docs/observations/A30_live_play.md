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

## 2026-10-05 (A56 implementation pass)

- **Code shipped:** Investigate logs seen subtype codes, probes each logged
  code once with `Read {kind: supply}` (ground supplies preferred over carried),
  stores scroll codes that apply, and nominates unread scrolls for free read.
- **Live confirmation:** `create` for `OlympuffWalker` still returned
  `character_cap_reached`; sandbox `Wren` returned `open_character_cap_reached`.
  No scroll subtype code was confirmed on the wire in this pass.
- **When live play opens:** Spot a scroll on the map or in a pack, note its
  `supply_subtype_code` here after a confirming `Read`, then remove the
  GAME_NOTES open question once a code is recorded.
