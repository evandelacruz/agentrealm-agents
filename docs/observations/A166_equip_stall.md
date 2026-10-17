# A166: Equip held with nothing sent for 30 s ashore

Gary, live run, after a crossing.

## What happened

- He stood ashore with the raft armed and the bronze sword held, at full health, no hostile in view.
- The top op was a planner `equip` with no `code` ("re-arm the bronze sword once ashore").
- For 30 s every tick read "Equip holds its walk". No `Arm` or `Wear` went out, and the op never finished ("nothing to equip").
- The planner put the same `equip` back on top once. The 30 s stall clock (A104) moved it down, and Travel's re-arm ashore (A133) armed the sword later.

## Why

The repro (`python/tests/test_equip_no_stall.py`) gives the path. A `would_strand` refusal had marked the shore cell as water (`Memory.water_cells`), filed against a position the character had already left for land. The water gate (`fording.ford_queue`) then read the cell as water with the raft armed and cut Equip's `Arm` raft→sword to nothing ("Arm would strand us on the water"). Equip fell through; Explore's hold for the op asked the planner once ("Equip has no move"), and once the planner answered with the same op, the ask counted as made and the hold sent nothing until the stall clock ran out.

## Rules (A166)

1. The top op's executor cut to nothing by the water gate asks the planner with the cut's why and holds.
2. A `would_strand` mark lapses after `fording.WATER_MARK_TICKS`; a cell the map shows as ground is then ground again, and a no-code `equip` arms the best weapon.
3. Any state that holds with no intent for `dispatch.HELD_ASK_SECONDS` (no reflex holding the round, no progress waited on, no ask of its op from that cell awaiting a reply) raises one `no_way_on` with the state's name and reason.
