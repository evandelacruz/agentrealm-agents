"""The oscillation guard: stop pacing back and forth between two cells (A15, A58).

Two states can fight over the character: one steps it east, the next decision
another steps it back west, and it paces between two cells for minutes. No
single state sees that, because each one only ever sends one sensible step.

So dispatch (``states/dispatch.py``) asks this module once per decision,
before any state runs. It remembers the cells the character stood on at the
last few decisions; when the last ``OSCILLATION_STEPS`` of them hold at most
2 distinct cells, the character is pacing. The movement target it is pacing
toward is then given up through stuck detection's own step 5
(``stuck.give_up``): backed off for ``BACKOFF_BASE_TICKS`` doubled on each
give-up, its path dropped, and a ``stuck`` signal queued. An ``oscillation``
event goes to the trace, and dispatch picks again with that target out of the
way. It works the same whichever states caused it.

Standing still never counts: a cell is remembered only when it differs from
the one before, so resting, fighting in place or a ``Use`` are not pacing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..world import Pos, WorldModel
from . import stuck as nav_stuck

if TYPE_CHECKING:
    from ..memory import Memory

# Decisions in a row, each on a new cell, that may hold only 2 distinct cells
# before we call it pacing: A, B, A, B, A, B. Six is three round trips, more
# than any honest detour (step aside for an occupant and back is one), and
# still only a few seconds of wasted play.
OSCILLATION_STEPS = 6
OSCILLATION_CELLS_MAX = 2

EVENTS_KEPT = 16  # newest kept until the runner writes them to the trace


def pacing(cells: list[Pos]) -> bool:
    """The last ``OSCILLATION_STEPS`` cells hold at most 2 distinct ones."""
    window = cells[-OSCILLATION_STEPS:]
    return len(window) == OSCILLATION_STEPS and len(set(window)) <= OSCILLATION_CELLS_MAX


def check(m: Memory, w: WorldModel) -> dict | None:
    """Note where the character stands; if it is pacing, give up what it walks to.

    Returns the ``oscillation`` event (also queued on
    ``m.nav_stuck.oscillations`` for the trace), or None.
    """
    stuck = m.nav_stuck
    if w.pos is None or w.map_id is None:
        return None
    if stuck.cells_map != w.map_id:
        stuck.cells_map, stuck.recent_cells = w.map_id, []
    if not stuck.recent_cells or stuck.recent_cells[-1] != w.pos:
        stuck.recent_cells.append(w.pos)
        del stuck.recent_cells[:-OSCILLATION_STEPS]
    if not pacing(stuck.recent_cells):
        return None

    cells = sorted(set(stuck.recent_cells))
    stuck.recent_cells = []
    # The target it paces toward: the active stuck attempt, or else the end
    # of the path it holds (a state that walks without tracking one).
    att = nav_stuck.active(m, w)
    if att is None and m.goal and m.path:
        att = nav_stuck.track(m, w, m.goal, m.path[-1])
    event = {"event": "oscillation", "tick": w.tick, "map_id": w.map_id, "cells": [list(p) for p in cells]}
    if att is not None:
        event.update(goal=att.goal, target=list(att.target))
        nav_stuck.give_up(m, w, att, "oscillation")
    else:
        m.path, m.goal = [], ""
    stuck.oscillations.append(event)
    del stuck.oscillations[:-EVENTS_KEPT]
    return event
