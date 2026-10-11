# A102: pack room (free-play run 13)

Two defects from free-play run 13, fixed by A102 (PLAN.md).

## Defect 1: a drop on the shop tile became shop stock

To make room for a bought potion, Shop dropped a box of matches on the shop
tile. The matches reappeared as shop stock priced 5 gems. The planner meant
those matches for two clue areas.

Cause: Shop made room with Loot's rule (`loot.pickup_room`), the lowest
`loot_score` held, which ranks gear by price, so the 5-gem matches went
before the 25-gem mallet. Nothing kept an item the plan would use, and
nothing checked the cell the drop landed on.

That a supply dropped on a shop cell turns into stock priced for sale is
this observation only: the Manual (Items, slots and gear) says `Drop` puts a
carried supply on the ground under you and says nothing of shop cells.

## Defect 2: the planner could not see pack space

State had `held` (A92) but no slot count, so the buy that needed a drop was
planned blind, while the pack held an unused 25-gem mallet and a spare knife
the planner could have chosen to give up.

## Fix

One rule, `pack.make_room`, for every state that picks up; State `pack`
shows slots used and total and the items the plan reserves; `buy` and
`fetch_item` name what to drop with `drop` when nothing held is plain junk.
