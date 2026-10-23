# A169: a blocked goal left Lorraine idle (Lorraine live run)

One defect from a Ladder Lorraine live run, fixed by A169 (PLAN.md).

## What happened

Short of her risk for the next level, with the gems for a potion, Lorraine
walked into town to the potion seller. Her walk was given up 5–12 cells
short of the seller, among the town's NPCs. The planner was then asked
about "ready for its risk" and answered `cannot`. For about 60 s she held at
the town: every tick record read "no op to work", nothing was sent, no
goal changed and nothing more was asked. She was not hurt and not attacked.

Over the whole run she held 575 s in all, in a cycle of about 70 s: the
planner was asked, she ran the buy, the walk read "given up" on the same
tick, the planner answered `cannot`, about 60 s of "no op to work" followed,
the block lapsed, and it began again. The first walk to the seller was
given up with `stalled: false` and `stalls: 0`, after shrinking to one-step
queues 7 cells west and 12 cells south of the seller's cell: the stall
counter did not give it up.

## Causes

0. **A rejection's lesson never lapsed.** A Step refused because an
   occupant stood on the cell marks it to keep off for one decision
   (`rejection.end_decision` ends it; Gary's dispatch runs it every
   decision). Lorraine's loop never ran it, so the cell stayed impassable
   for good. With the way to the seller shut by it, every level of the stuck
   ladder failed on "no path" in a few rounds, reveal walked its one-step
   queues, and the ladder gave the target up. Occupant rejections also
   counted toward the reject streak, as a wall's do.
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
2a. **The given-up seller was chosen again.** Every later round her buy
   walked to the same seller, whose walk was given up and still backed
   off, so it was over on the same tick; the planner was not told the walk
   was given up, so its answers pointed back at it too.
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
- "No op to work" always asks the planner, with the reason; exploring only
  fills the wait for the answer.
- A given-up walk target is not chosen again until its backoff ends, by the
  tree or the planner, who is told which targets are given up and why.
- A blocked node's siblings are worked first, in tree order, then the nodes
  below it.
- An occupant's rejection lasts one decision and counts toward no stall.
- Occupants are transient: a walk goes round them, waits a bounded time for
  them to clear (a wait the clock clears), or stands at the free cell within
  reach of a person it walks to. Only terrain makes a target unreachable.
- Each tick record names the goals blocked and why beside the goal worked.
