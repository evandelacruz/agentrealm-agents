# A20 live play (redacted)

Character: `A20LootObserver` on `olympuff` (public API). Supply and entity ids are omitted.

## Ground gem (`supply_subtype_code`)

- **Code:** `gem` (no `gem_price` on free pickups).
- **Evidence:** Entity reads in town (map 76) list ground supplies with `supply_subtype_code` `gem`, matching prior observation in [GAME_NOTES.md](../GAME_NOTES.md) (GemPileObserver0c52; grass and bush drops use the same code).
- **Take:** Confirmed only when adjacent (`target_not_nearby` otherwise). This session could not walk to a pile after the character became movement-blocked (`not_traversable` on every `Step`/`SetPosition` from `(360, 386)`).

## Ground life (heart)

- **Code:** Not observed.
- **Session:** 400+ `Use` cuts on grass at ring-1 town coordinates with pocket knife; many `gem` drops seen on entity reads; no pickup increased `lives` above 10 and no ground supply appeared with a code other than `gem` / town food (`apple`, `berry`) in that loop.
- **Still open:** [GAME_NOTES.md](../GAME_NOTES.md) open question — which `supply_subtype_code` a life has on the ground.

## Stowed `Drop`

- **Not observed.** `DepositToChest` in the [API](https://agentrealm.gg/docs/api) targets a **ground** chest (`chest_id` + `supply_ids`), not the carried chest’s `inventory.chest`. This session did not get an adjacent free pickup after the movement block, so stowed-only `Drop` was not exercised.
- **Still open:** [GAME_NOTES.md](../GAME_NOTES.md) — whether `Drop` accepts a supply id stowed in the carried chest.

## API notes

- Rate limit: burst exceeded when pacing &lt; one call per tick window; observation script uses ~0.11s between calls and retries on `429`.
