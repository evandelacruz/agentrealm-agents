# Lorraine's first live run

Ladder Lorraine (`lorraine`, `python/ladder/`), her first live run on the overworld, planner on.

## The silent hold (A168)

- About 11 s in she took one gem. Her gather walk then retargeted to the next gem in sight, a ground supply with two hostiles 1–5 cells from it.
- The walk that keeps off hostile ground (`pathing.guided_step` with the op walk's reach, A128) had no step, and held.
- Every round trip from then on, for 70 s and more, read `gather_gems: lorraine:gather: no step toward (x, y)` with nothing sent.
- In that time: no planner ask, no `cannot`, no target give-up, no goal change, no reflex. She stood at 8/10 health and was not attacked.

What it showed: an op that sends nothing and is not over held the round for ever, and nothing above it noticed. A168 makes that impossible by rule, not for this case: a walk with no step gives its target up and is over, an op with many targets takes the next one that has a step, hostile-adjacent gather targets come last and only within her risk budget, the same empty round never repeats, a stall guard blocks a goal after `STALL_ROUNDS` empty rounds and asks the planner, and every tick record names the goal, op, act result and holding reflex (PLAN.md A168).
