# A173: a cut that changed nothing, sent 143 times (Lorraine run 4)

One defect from Ladder Lorraine's fourth live run (main fbe3308, after A170,
A171 and A172), fixed by A173 (PLAN.md).

## What happened

About 1 min in she stood on a grass cell in town, a safe cell, with goal
gather and op `gather_gems`, and sent "cut (421, 379)", a `Use` on the cell
she stood on, 143+ times over 2+ min. No `BlockChanged` came back and no
events; every tick record read `stalled: false`, `stalls: 0`. She held there
until the run was stopped. The runner's read of the trace: town grass inside
the safe zone (`safe: true`), 197 `Use`s on (421, 379) and 13 on (424, 376);
a probe's queue finished with no result entry for the `Use`, no events and no
rejection, the block still grass. So a strike that changes nothing may bring
back no result code at all, and only the observation shows it.

## Why

A `Use` on a block that cannot be destroyed is `applied_no_effect` (Manual
§11 Breaking blocks), as is a strike on a block already broken and not yet
grown back. Her loop never read a strike's result against what it was for,
so the cell kept its region's yield in gather's value and stayed the best
target, at no walk. A168's guard ends an op only when it sends nothing, and
this one sent every round.

## The rule

Every act's result is read against what it was for, from the observation
as well as the result code: a strike whose queue ran with no `BlockChanged`
on its cell changed nothing, whatever result came back or none. A strike that changed
nothing notes its cell as not yielding now, with why (regrowing until its
timer, never breaking, or not breaking with what was in hand), in the ADK's
break memory; the value reads the note, so the cell is worth nothing until
it lapses and gather takes the next-best target, with no case for town. The
same act with no effect three times in a row ends its op as "changed
nothing", so no op loops on a no-op act. Gary's Gather reads the same notes.
