"""The oscillation guard: stop pacing back and forth between two cells (A15, A58).

Two states can fight over the character: one steps it east, the next decision
another steps it back west, and it paces between two cells for minutes. No
single state sees that, because each one only ever sends one sensible step.

So dispatch (``states/dispatch.py``) asks this module once per decision,
before any state runs. It remembers the cells the character stood on at the
last few decisions; when the last ``OSCILLATION_STEPS`` of them hold at most
2 distinct cells, the character is pacing. Every walk that made one of the
paced moves is then given up, not only the one walked last: while the other
walk pulls it back, none of them can get nearer. A walk is given up through
stuck detection's own step 5 (``stuck.give_up``, reason ``pacing``): backed
off for ``BACKOFF_BASE_TICKS`` doubled on each give-up, its path and
commitment dropped, and a ``stuck`` signal queued. Break's walk to the block
a stuck attempt's step 2 nominated is that attempt's walk too: when only
the break walk moved, the ladder leaves step 2 (``stuck.escalate``, reason
``pacing``) and the block is remembered as a stalled ``break_block`` in
``Memory.stalled_ops`` (A104), so no walk is sent to open it again until
something gives a reason to retry (``stalled.break_cells``, A111). An
``oscillation`` event goes to the trace, and dispatch picks again with those
targets out of the way. It works the same whichever states caused it.

Giving up a walk target is not enough when the plan's top op's executor is
one of the pacers: each step it sends counts as progress, so the op never
stalls, and the other state just walks to its next target. Dispatch then
treats that op as stalled: remembered, and moved below the next op that can
make progress or dropped when none can, with a why that tells the planner
which states paced between which cells (``states/dispatch.py``
``_stall_paced_op``, A103, A104).

After each decision dispatch also calls ``note_move``, so the guard knows
which walk, and which stuck attempt, made each move. A target is given up
only when its own walk was one of the moves: states that pace with steps of
their own (Retreat, Fight, Flee) back nothing off. Heal's and Loot's walks are stuck attempts of their
own (``pathing.bounded_step``), so pacing on one of them gives up its own
food, pickup or safe tile.

Survival-only pacing is corrected here too, in the same one place. When
only Flee and Retreat made the moves, the guard hands the paced cells to
them (``take_escape``) for that decision only: Flee plans a fresh
committed escape (A9) that may not step back onto either cell, or Retreat
replans its path to safety around them. Neither state needs a pacing
patch of its own.

Standing still never counts: a cell is remembered only when it differs from
the one before, so resting, fighting in place or a ``Use`` are not pacing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..stalled import remember as remember_stalled, retry_basis
from ..world import Pos, WorldModel
from . import stuck as nav_stuck

if TYPE_CHECKING:
    from ..knowledge_base import KnowledgeBase
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


def check(m: Memory, w: WorldModel, knowledge: KnowledgeBase | None = None) -> dict | None:
    """Note where the character stands; if it is pacing, give up what it walks to.

    Every stuck attempt whose walk made some of the moves (``note_move``) is
    given up (``_give_up_walks``). When other states did all the moving
    (Retreat, Fight or Flee pacing on their own), a goto or explore attempt
    left over from earlier did not cause it, so nothing is backed off; the
    event still goes to the trace.

    When only Flee and Retreat did the moving, the paced cells are kept for
    ``take_escape`` and the event carries ``escape: true``. They hold for
    this decision only: the next ``check`` clears them, read or not.

    Returns the ``oscillation`` event (also queued on
    ``m.nav_stuck.oscillations`` for the trace), or None. It names each walk
    given up in ``goals`` (the first also as ``goal`` and ``target``).
    """
    stuck = m.nav_stuck
    stuck.escape_from = set()  # held only for the decision the guard fires on
    if w.pos is None or w.map_id is None:
        return None
    if stuck.cells_map != w.map_id:
        stuck.cells_map, stuck.recent_cells, stuck.recent_moves = w.map_id, [], []
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
    states = sorted({state for _, state, _ in moves if state})
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
    given = _give_up_walks(m, w, moves, cells, knowledge)
    if given:
        event.update(goal=given[0]["goal"], target=given[0]["target"], goals=[g["goal"] for g in given])
        breaks = [g["break"] for g in given if "break" in g]
        if breaks:
            event["breaks"] = breaks
    stuck.oscillations.append(event)
    del stuck.oscillations[:-EVENTS_KEPT]
    return event


def _give_up_walks(
    m: Memory, w: WorldModel, moves: list[tuple[str, str, str]], cells: list[Pos], knowledge: KnowledgeBase | None
) -> list[dict]:
    """Give up each stuck attempt on this map whose walk made one of ``moves``.

    An attempt whose own walk moved is given up (``stuck.give_up``). One
    whose only moves were Break's walk to the block its step 2 nominated
    leaves step 2 instead (``stuck.escalate``): that block is what cannot be
    got nearer, and it is remembered as a stalled ``break_block`` (A104,
    A111), so neither this attempt's ladder nor a later one sends Break to
    it again until something gives a reason to retry. Returns what was given
    up, in the order the walks first moved.
    """
    stuck = m.nav_stuck
    given: list[dict] = []
    for key in dict.fromkeys(k for _, _, k in moves if k):
        att = stuck.attempts.get(key)
        if att is None or att.map_id != w.map_id:
            continue
        walked = {goal for goal, _, k in moves if k == key}
        entry = {"goal": att.goal, "target": list(att.target)}
        if att.goal in walked:
            nav_stuck.give_up(m, w, att, GIVE_UP_REASON)
        elif att.level == nav_stuck.BREAK:
            block = nav_stuck.break_target(att)
            if block is not None and att.break_cap:
                op = {"op": "break_block", "x": block[0], "y": block[1], "capability": att.break_cap}
                between = " and ".join(f"{x},{y}" for x, y in cells)
                why = f"Break paced between {between} walking to it: no way to the block"
                remember_stalled(m.stalled_ops, op, why, w.tick, retry_basis(w, knowledge), w.map_id)
                entry["break"] = list(block)
            nav_stuck.escalate(m, w, att, GIVE_UP_REASON)
        else:
            continue
        given.append(entry)
    return given


def note_move(m: Memory, intents: list[dict] | None, state: str) -> None:
    """After a decision: which walk, and which stuck attempt, the move it sends belongs to.

    The goal of ``m.path`` when the first move lands on the path's next cell,
    else "" (a step of its own, such as a Fight approach or a detour). A
    step that happens to land on the next cell of a path another state left
    behind counts for that path's walk. The attempt is the active one when
    the walk is its own, or Break's walk to the very block the attempt's
    step 2 nominated (``walk_attempt``). The next ``check`` files it with the cell
    the move led to.
    """
    target = None
    for intent in intents or []:
        if intent.get("verb") == "SetPosition":
            target = (intent["x"], intent["y"])
            break
    goal = m.goal if target is not None and m.path[:1] == [target] else ""
    m.nav_stuck.last_move = (goal, state if intents else "", walk_attempt(m, goal))


def walk_attempt(m: Memory, goal: str) -> str:
    """The key of the active stuck attempt the walk ``goal`` belongs to, or "".

    A break walk is the attempt's only when the attempt is at step 2 and the
    walk ends on the block it nominated: a ``break_block`` op's walk or
    Heal's cut toward food (``Memory.goal`` ``break`` too) to any other
    block is no attempt's.
    """
    stuck = m.nav_stuck
    att = stuck.attempts.get(stuck.active) if goal and stuck.active else None
    if att is None:
        return ""
    if att.goal == goal:
        return att.key
    block = nav_stuck.break_target(att)
    if goal == nav_stuck.BREAK_GOAL and att.level == nav_stuck.BREAK and block is not None and m.path[-1:] == [block]:
        return att.key
    return ""


def take_escape(m: Memory, w: WorldModel) -> set[Pos]:
    """The cells survival-only pacing just ran between, other than the one we stand on; cleared once read.

    Flee and Retreat call this when they act; a non-empty answer means the
    guard caught them pacing and the escape they plan now must avoid these
    cells (module docstring).
    """
    stuck = m.nav_stuck
    cells, stuck.escape_from = stuck.escape_from, set()
    return cells - {w.pos}
