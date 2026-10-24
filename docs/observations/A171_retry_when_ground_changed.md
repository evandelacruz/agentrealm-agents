# A171: a given-up walk retried on time alone (Lorraine run 3)

One defect from Ladder Lorraine's third live run, fixed by A171 (PLAN.md).

## What happened

From about 2 min in, her level node chose `enter_level` at the only known
level-1 entrance. The walk there was given up 21 times in about 9 min: 17
read "walk given up" and 4 "no step". Each give-up was retried about 30 s
later, when A169's timed give-up lapsed, and nothing round the entrance had
changed between one try and the next. Asked about the node, the planner
answered `cannot`: the entrance "can't be reached … boxed in".

## Why

A169 retried a blocked goal once the backoff of a walk given up for it
ended (`Block.retry_at`, the soonest backoff end among the give-ups since
the goal began), and the ADK's give-up memory lapsed on time alone. A walk
shut by terrain stays shut until the ground changes, so every retry ran the
same search on the same ground and gave up again.

## The rule

A given-up walk is retried only once the known ground that could open a way
has changed: cells seen round her walk and its target, a block opened or
broken, an occupant gone, or a capability gained (`navigation.stuck.Ground`,
`note_ground`). Time alone does not lift it. While it holds, her goal stays
blocked and the planner is asked once for another way: explore toward the
target, another entrance, or what opens the way. Gary's walks follow the
same rule, since it lives in the ADK.
