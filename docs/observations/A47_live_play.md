# A47 live play (redacted)

Dedicated character **loot-observer-fixture** (Olympuff, map 76) and **smoke-character-a** (shop
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
- **Still open (A57):** which code a life pickup uses on Olympuff (see [A20_live_play.md](A20_live_play.md), [A57_live_play.md](A57_live_play.md)).

## Stowed `Drop`

- **Not observed.** No run filled `inventory.chest` with a droppable supply: free pickups were out
  of reach or `Take` was `target_not_nearby`, and account characters could not be created
  (`character_cap_reached`).
- **Still open (A57):** whether `Drop` accepts a supply id listed only in `inventory.chest` ([A57_live_play.md](A57_live_play.md)).

## Larger chest (`middle_chest`)

- **Shop seen:** wide entity read at the town shop listed `middle_chest` priced at 50 gems
  (Manual §16 table: capacity 30). Test characters had 6–12 gems; no purchase was attempted.
- **Still open (A57):** measured `carry_capacity_full` slot count after buying and filling with a
  `middle_chest` upgrade ([A57_live_play.md](A57_live_play.md)).

## Session 3 (A57 pass, 2026-10-05)

Dedicated characters **loot-observer-fixture** (Olympuff, map 76), **smoke-character-a** (528,406,
6 gems), **smoke-character-c** (386,395, 12 gems), and sandbox **sandbox-character-fixture** (map 14,
66,63). Tick POSTs only for movement and cuts where noted; entity reads spaced ≥1.2 s apart.

- **Life code:** Hundreds of paced `Use` calls on grass at (364,381) and adjacent cells returned
  `applied_no_effect` once the standing tile was `dirt`; no supply entities appeared in snapshot
  entities or in a spaced entity read after cuts. `lives` stayed 10.
- **Stowed `Drop`:** No observer had a non-empty `inventory.chest`; wide entity reads near town
  plaza and sandbox start found zero free supplies in sight, so nothing was taken or deposited.
- **Middle chest:** No character held ≥50 gems; shop `middle_chest` was not purchased.
- **Rate limits:** `GET …/entity-tiles` in the same burst as walk `Step` queues hit `429`;
  retry after `Retry-After` succeeded.

## API notes

- Stay near one request per tick window; retry on `429`.
- Wake sleeping observers with `Wait` before walking.
