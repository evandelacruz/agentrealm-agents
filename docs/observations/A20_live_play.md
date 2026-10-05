# A20 live play (redacted)

Character: `A20LootObserver` on `olympuff` (public API), played by hand-run API calls. Supply and entity ids are omitted.

## Ground gem (`supply_subtype_code`)

- **Code:** `gem`, with no `gem_price` on free ground supplies.
- **Evidence:** Entity reads in town (map 76) listed ground supplies with `supply_subtype_code` `gem`. This matches the earlier observation in [GAME_NOTES.md](../GAME_NOTES.md) (Authored gem piles on the wire, GemPileObserver0c52).
- **Take:** A `Take` from out of reach was refused `target_not_nearby`. No adjacent `Take` of a gem was made: the character became movement-blocked (`not_traversable` on every `Step`/`SetPosition` from `(360, 386)`) before reaching a pile.

## Ground life (heart)

- **Code:** Not observed.
- **Session:** 400+ `Use` cuts on grass at ring-1 town coordinates with the pocket knife. Entity reads showed `gem` drops; no ground supply appeared with a code other than `gem`, `apple` or `berry`.
- **Still open:** [GAME_NOTES.md](../GAME_NOTES.md) open question: which `supply_subtype_code` a life has on the ground.

## Stowed `Drop`

- **Not observed.** No stowed supply was available to try it on.
- **Still open:** [GAME_NOTES.md](../GAME_NOTES.md): whether `Drop` accepts a supply id stowed in the carried chest.
