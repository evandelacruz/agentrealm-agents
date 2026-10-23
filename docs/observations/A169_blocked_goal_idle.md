# A169: a blocked goal left Lorraine idle (Lorraine live run)

One defect from a Ladder Lorraine live run, fixed by A169 (PLAN.md).

## What happened

Short of her risk for the next level, with the gems for a potion, Lorraine
walked into town to the potion seller. Her walk was given up 5–12 cells
short of the seller, among the town's NPCs. The planner was then asked
about "ready for its risk" and answered `cannot`. For about 60 s she held at
the town: every tick record read "no op to work", nothing was sent, no
goal changed and nothing more was asked. She was not hurt and not attacked.

## Causes

1. **Occupants ended the walk.** NPCs stood on the last cells of the path
   to the seller. The walk's first step was taken, so it waited; under A168
   a round that sends nothing with the same why as the round before ends
   the op, and an occupant's wait was counted as such a repeat. Waited out
   long enough, the same wait would also have failed the ADK stuck ladder's
   time window at each level until the target was given up, as if a wall
   stood there.
2. **Nothing under the blocked goal had an op.** Below a blocked node the
   walk skipped every ready child, so with the gems in hand "hold the gems"
   was ready and "gather" was never reached; and "gather" itself had no op
   with no gems short. The walk gave no goal.
3. **No goal was a silent hold.** With no goal, the loop asked the planner
   only about blocks it had set itself; a planner's `cannot` counted as its
   answer already, so nothing was asked and nothing explored.

## Rules (A169)

- A blocked goal does not end the tree's work: the walk works the next node
  not ready under it, in the tree's order, and "gather" is always workable
  while a gather target exists. The blocked goal is retried once its blocker
  can have changed: a walk given up for it may be walked again, the state
  the planner saw (region, what she carries, health) changed, or the block's
  expiry.
- "No op to work" is never a silent hold: she explores, then asks the
  planner with the reason.
- Occupants are transient: a walk goes round them, waits a bounded time for
  them to clear (a wait the clock clears), or stands at the free cell within
  reach of a person it walks to. Only terrain makes a target unreachable.
- Each tick record names the goals blocked and why beside the goal worked.
