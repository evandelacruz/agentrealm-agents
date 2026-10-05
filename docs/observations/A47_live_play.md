# A47 live play (redacted)

Dedicated character **A20LootObserver** (Olympuff, map 76) and **OlympuffM6Walk5d4e** (shop
area). Supply and entity ids are omitted where noted. Sessions on 2026-10-05 against the public
API.

## Technique

- Movement queues must stay within the horizon (40 intents at 10 Hz): chunk `Step` plus four `Wait`s, then
  drain with `POST tick` omitting `intents` so the server runs the queue.
- Replacing the queue every poll before it drains leaves the character stuck; clearing with `[]`
  then re-queuing works.

## Ground life (`supply_subtype_code`)

- **Not observed.** Grass `Use` samples (40–80 cuts) at town coordinates produced no new codes in
  entity deltas; `lives` stayed 10.
- **Still open:** which code a life pickup uses on Olympuff (see [A20_live_play.md](A20_live_play.md)).

## Stowed `Drop`

- **Not observed.** No run filled `inventory.chest` with a droppable supply: free pickups were out
  of reach or `Take` was `target_not_nearby`, and account characters could not be created
  (`character_cap_reached`).
- **Still open:** whether `Drop` accepts a supply id listed only in `inventory.chest`.

## Larger chest (`middle_chest`)

- **Shop seen:** wide entity read at the town shop listed `middle_chest` priced at 50 gems
  (Manual §16 table: capacity 30). Test characters had 6–12 gems; no purchase was attempted.
- **Still open:** measured `carry_capacity_full` slot count after buying and filling with a
  `middle_chest` upgrade.

## API notes

- Stay near one request per tick window; retry on `429`.
- Wake sleeping observers with `Wait` before walking.
