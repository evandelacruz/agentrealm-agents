# A20 live play (redacted)

Characters on `olympuff` via the public API. Supply and entity ids are omitted.

## Ground gem (`supply_subtype_code`)

- **Code:** `gem`, with no `gem_price` on free ground supplies.
- **Evidence:** Entity reads and observation deltas in town (map 76) list ground supplies with `supply_subtype_code` `gem`. Matches [GAME_NOTES.md](../GAME_NOTES.md) (GemPileObserver0c52; grass and bush drops use the same code when a gem lands on the ground).
- **Take:** Adjacent `Take` of a `gem` increments the gems counter; `lives` unchanged (10→10 on A20LootObserver).

## Ground life (heart)

- **Code:** Not observed.
- **Session 1 (A20LootObserver):** 400+ `Use` cuts on grass at ring-1 town coordinates with the pocket knife. Drops on entity reads were `gem`, `apple`, and `berry` only.
- **Session 2 (A20LootObserver, map 76, town plaza ~360–363, 386–387):** 1500+ `Use` cuts on grass at the standing cell; observation deltas showed added supplies with codes `gem` and `berry` only. One adjacent `Take` of `gem` did not change `lives`. 300+ `Use` cuts on nearby bushes did not surface a new code in deltas (movement in this pocket is mostly cutting in place; `SetPosition` often rejects `beyond_movement_range` when a prior queue is still resolving).
- **Session 2 (A20CounterObserver, map 76, ~391,385):** Short run interrupted by API 502; no life code before stop.
- **Manual cross-check:** [Manual §16](https://agentrealm.gg/docs/manual) lists **gem** drops from cutting Olympuff grass and bushes (10% ring 1, 15% farther). It lists gems **and hearts** from **sandbox** field grass and bushes when broken (often with a bomb), not explicitly from Olympuff grass.
- **Still open (A47):** [GAME_NOTES.md](../GAME_NOTES.md) — which `supply_subtype_code` a life has on the ground, and whether Olympuff field grass ever drops a ground life supply on the wire.

## Stowed `Drop`

- **Not observed.** No stowed supply was available to try it on.
- **Still open (A47):** [GAME_NOTES.md](../GAME_NOTES.md) — whether `Drop` accepts a supply id stowed in the carried chest.

## API notes

- Rate limit: stay near one call per tick window; retry on `429`.
- Wake a sleeping character with any intent (`Wait` is enough) before `GET position`.
