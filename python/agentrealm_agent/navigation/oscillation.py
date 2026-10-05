"""The oscillation guard: stop pacing back and forth between two cells (A15, A58).

Two states can fight over the character: one steps it east, the next decision
another steps it back west, and it paces between two cells for minutes. No
single state sees that, because each one only ever sends one sensible step.

So dispatch (``states/dispatch.py``) asks this module once per decision,
before any state runs. It remembers the cells the character stood on at the
last few decisions; when the last ``OSCILLATION_STEPS`` of them hold at most
2 distinct cells, the character is pacing. The navigation target being
walked to is then given up through stuck detection's own step 5
(``stuck.give_up``, reason ``pacing``): backed off for ``BACKOFF_BASE_TICKS``
doubled on each give-up, its path dropped, and a ``stuck`` signal queued. An
``oscillation`` event goes to the trace, and dispatch picks again with that
target out of the way. It works the same whichever states caused it.

After each decision dispatch also calls ``note_move``, so the guard knows
which walk made each move. A target is given up only when its own walk was
one of the moves: survival states (Retreat, Fight, Flee, Heal) that pace on
their own back nothing off.

Survival-only pacing is corrected here too, in the same one place. When
only Flee and Retreat made the moves, the guard hands the paced cells to
them (``take_escape``): the next Flee plans a fresh committed escape (A9)
that may not step back onto either cell, and the next Retreat replans its
path to safety around them. Neither state needs a pacing patch of its own.

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

# The ``stuck`` signal's reason, distinct from stuck detection's own
# per-attempt "oscillation" (``stuck.stuck_reason``).
GIVE_UP_REASON = "pacing"

# States whose pacing on their own is corrected by a forced committed escape
# (``take_escape``) rather than by giving anything up.
ESCAPE_STATES = frozenset({"Flee", "Retreat"})


def pacing(cells: list[Pos]) -> bool:
    """The last ``OSCILLATION_STEPS`` cells hold at most 2 distinct ones."""
    window = cells[-OSCILLATION_STEPS:]
    return len(window) == OSCILLATION_STEPS and len(set(window)) <= OSCILLATION_CELLS_MAX


def check(m: Memory, w: WorldModel) -> dict | None:
    """Note where the character stands; if it is pacing, give up what it walks to.

    Only the active stuck attempt is given up, and only when its own walk
    made some of the moves (``note_move``). When other states did all the
    moving (Retreat, Fight, Flee or Heal pacing on their own), a goto or
    explore attempt left over from earlier did not cause it, so nothing is
    backed off; the event still goes to the trace.

    When only Flee and Retreat did the moving, the paced cells are kept for
    ``take_escape`` and the event carries ``escape: true``.

    Returns the ``oscillation`` event (also queued on
    ``m.nav_stuck.oscillations`` for the trace), or None.
    """
    stuck = m.nav_stuck
    if w.pos is None or w.map_id is None:
        return None
    if stuck.cells_map != w.map_id:
        stuck.cells_map, stuck.recent_cells, stuck.recent_moves = w.map_id, [], []
        stuck.escape_from = set()
    if not stuck.recent_cells or stuck.recent_cells[-1] != w.pos:
        stuck.recent_cells.append(w.pos)
        stuck.recent_moves.append(stuck.last_move)
        del stuck.recent_cells[:-OSCILLATION_STEPS]
        del stuck.recent_moves[:-OSCILLATION_STEPS]
    if not pacing(stuck.recent_cells):
        return None

    cells = sorted(set(stuck.recent_cells))
    # The moves between the window's cells; the first cell's own move led
    # into the pacing, it is not part of it.
    moves = stuck.recent_moves[1:]
    walks = {goal for goal, _ in moves if goal}
    states = sorted({state for _, state in moves if state})
    stuck.recent_cells, stuck.recent_moves = [], []
    event = {
        "event": "oscillation",
        "reason": GIVE_UP_REASON,
        "tick": w.tick,
        "map_id": w.map_id,
        "cells": [list(p) for p in cells],
        "states": states,
    }
    if states and set(states) <= ESCAPE_STATES:
        stuck.escape_from = set(cells)
        event["escape"] = True
    att = nav_stuck.active(m, w)
    if att is not None and att.goal in walks:
        event.update(goal=att.goal, target=list(att.target))
        nav_stuck.give_up(m, w, att, GIVE_UP_REASON)
    stuck.oscillations.append(event)
    del stuck.oscillations[:-EVENTS_KEPT]
    return event


def note_move(m: Memory, intents: list[dict] | None, state: str) -> None:
    """After a decision: which walk the move it sends belongs to.

    The goal of ``m.path`` when the first move lands on the path's next cell,
    else "" (a step of its own, such as a Fight approach or a detour). A
    step that happens to land on the next cell of a path another state left
    behind counts for that path's walk. The next ``check`` files it with the
    cell the move led to.
    """
    target = None
    for intent in intents or []:
        if intent.get("verb") == "SetPosition":
            target = (intent["x"], intent["y"])
            break
    on_path = target is not None and m.path[:1] == [target]
    m.nav_stuck.last_move = (m.goal if on_path else "", state if intents else "")


def take_escape(m: Memory, w: WorldModel) -> set[Pos]:
    """The cells survival-only pacing just ran between, other than the one we stand on; cleared once read.

    Flee and Retreat call this when they act; a non-empty answer means the
    guard caught them pacing and the escape they plan now must avoid these
    cells (module docstring).
    """
    stuck = m.nav_stuck
    cells, stuck.escape_from = stuck.escape_from, set()
    return cells - {w.pos}
