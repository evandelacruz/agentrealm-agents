# A57 live play (redacted)

Dedicated characters **OlympuffM6Walk5d4e** (cid 10, Olympuff map 76) and **OlympuffWalker** (cid 4),
with earlier context from **GemPileObserver0c52** (cid 7) and sandbox **CursorA18Armor8774** (cid 8).
Sessions on 2026-10-05 against the public API. Supply ids are omitted.

## Technique

Same as [A47_live_play.md](A47_live_play.md): chunk movement with `SetPosition` or paced `Step`, four
`Wait`s, then drain with `POST tick` omitting `intents`. Clear a stuck queue with `intents: []`
before re-queuing. Space `GET …/entity-tiles` at least ~1 s from burst movement to avoid `429`.

## Ground life (`supply_subtype_code`)

- **Not confirmed.** On **OlympuffM6Walk5d4e** in ring-1 fields (~471,328 then ~430,392), 250+
  paced `Use` calls on adjacent `grass` cells produced ground supplies with codes `gem`, `berry`, and
  once `small_potion` in snapshot entities; `lives` stayed 10 throughout.
- **Take** of adjacent `gem` supplies raised the gems counter (6→10 over the session); no pickup
  raised `lives`.
- **Still open (A58):** wire `LIFE_SUPPLY_CODES` once a `Take` raises `lives` and A47's
  `learn_life_code` files the code (or sandbox field grass with a bomb per Manual §16).

## Stowed `Drop`

- **Not observed.** No run filled `inventory.chest` with a droppable supply. Town `apple` `Take`s
  from **GemPileObserver0c52** did not add `held` or `chest` rows in the snapshot (likely eaten on
  pickup at full health). Shop priced supplies were in sight from **OlympuffM6Walk5d4e** at
  (439,404) and (430,395) — e.g. `matches` 5 gems at (414,402), `middle_chest` 50 at (416,402) —
  but walking onto shop cells from town/plaza rows was often blocked by `wall` tiles; buys were not
  completed in this pass.
- **Still open (A58):** `Drop` a supply id listed only in `inventory.chest` and record applied vs
  `not_held` (store result in the knowledge base if accepted).

## Larger chest (`middle_chest`)

- **Not measured.** **OlympuffM6Walk5d4e** held 10 gems; **Pippin Thistledown** 12; no character
  reached 50 gems or applied a `Take` of `middle_chest`. Manual §16 cap (30) remains the assumed
  default in code until measured.
- **Still open (A58):** buy `middle_chest`, fill until `carry_capacity_full`, and replace
  `MANUAL_CHEST_CAPACITY` for `middle_chest` with the measured slot count.

## API notes

- `Take` of a distant gem from behind a wall row can answer `not_traversable` (permanent).
- Rate limit: retry `429` on entity reads; keep one intent per tick window for `POST tick`.
