# A165: died under attack with a potion held (redacted)

A live Gary run. Character chosen at run time; no name or id is committed.

## What happened

- On the walk to town a pack of two hostiles (a bogmaw and a wartlurch) closed on him.
- Health went 10 → 5 → 3 → 1 → dead over about 100 ticks. He held a small potion (heals 10) and the low line (`low_health`) was health ≤ 5. He never drank.
- Retreat held the round every tick ("queue under way"); no Heal, Flee or Fight tick ran.
- Retreat's refuge was a safe cell about 220 cells away. Its first step went up_left, toward the pack standing 2–4 cells NW. The chasers then kept pace 2–4 cells behind the whole way.
- At health 1, with the hostiles 4–6 cells away (out of `hostile_range`), Heal ran and broke a berry bush for food (heal 1). He died on the next hit.

## Why

1. Retreat ran above Heal and held the round while its walk queue ran. It drank only when losing ground, measured as the distance to its refuge not shrinking; walking toward a cell 220 away, that distance always shrank.
2. Heal ran only with no hostile in range, and its drink was never a reflex, so the held-queue probe (A64) would not have sent it over Retreat's queue anyway.
3. Retreat's path weighs no danger from the hostiles it runs from (`pursuer_peaks`), so its straight line to a refuge west of the pack went through the pack's side.
4. With the pack out of range and no hit in the last 30 ticks, Heal counted itself out of combat and cut a bush for berries ahead of the potion.

## Fixed by A165

See the A165 row in [PLAN.md](../../PLAN.md): drink first under attack at the low line (Heal above Retreat, as a reflex); no food under attack; Retreat's first step never toward a closing hostile; a chase that keeps pace under hits is running not working, and a fight the engagement then takes is fought instead of a walk to a far refuge.
