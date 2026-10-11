"""The oscillation guard: stop pacing back and forth between two cells (A15, A58, A90).

Two walks can fight over the character: one steps it east, the next decision
another steps it back west, and it paces between two cells for minutes. No
single state sees that, because each one only ever sends one sensible step.

So dispatch (``states/dispatch.py``) asks this module once per decision,
before any state runs. It remembers the cells the character stood on at the
last few decisions, and the walk each move into them belonged to; when the
last ``OSCILLATION_STEPS`` cells hold at most 2 distinct ones, the
character is pacing, and while one walk pulls it back none of them can get
nearer. So every walk that made one of the paced moves is given up,
whichever state sent it and whichever stuck attempt was active: a walk is
known by the target it heads for (``walk_key``), not by the state or
ladder level that sent it. It is given up through the ladder's own step 5
(``stuck.give_up``, reason ``pacing``): remembered with a backoff, its path
and commitment dropped, and a ``stuck`` trigger raised, so the planner
decides whether the goal behind it is still worth it. Every walk asks
whether its target is backed off, so none of them walks there again until
the backoff ends.

A walk to a block is a walk like any other. When the block a stuck
attempt's step 2 would open is a target given up, that attempt leaves step 2
(``stuck.escalate``, reason ``pacing``), and no ladder nominates the block
again while it is backed off (``break_memory.given_up_blocks``).

An ``oscillation`` event goes to the trace, and dispatch picks again with
those targets out of the way. When the plan's top op's executor made a
paced move that was no walk, nothing of its was given up, so dispatch
stalls the op instead (``states/dispatch.py`` ``_stall_paced_op``, A104).

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
    """Note where the character stands; if it is pacing, give up every walk that paced.

    Each walk that made one of the paced moves (``note_move``) is given up
    (``_give_up_walks``). When only steps that are no walk did the moving
    (a Fight approach), nothing is backed off; the event still goes to the
    trace.

    When only Flee and Retreat did the moving, nothing is given up: the
    paced cells are kept for ``take_escape`` and the event carries
    ``escape: true``. They hold for this decision only: the next ``check``
    clears them, read or not.

    Returns the ``oscillation`` event (also queued on
    ``m.nav_stuck.oscillations`` for the trace), or None. ``walks`` lists
    each walk given up with the states that walked it, ``goals`` their goals
    (the first also as ``goal`` and ``target``), and ``escalated`` the stuck
    attempts that left step 2 because their block was given up.
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
    event: dict = {
        "event": "oscillation",
        "reason": GIVE_UP_REASON,
        "tick": w.tick,
        "map_id": w.map_id,
        "cells": [list(p) for p in cells],
        "states": states,
        "unwalked": sorted({state for goal, state, key in moves if state and not key}),
    }
    if states and set(states) <= ESCAPE_STATES:
        stuck.escape_from = set(cells)
        event["escape"] = True
        given, escalated = [], []  # the escape is planned afresh around the cells instead
    else:
        given, escalated = _give_up_walks(m, w, moves)
    if given:
        event.update(goal=given[0]["goal"], target=given[0]["target"], goals=[g["goal"] for g in given], walks=given)
    if escalated:
        event["escalated"] = escalated
    stuck.oscillations.append(event)
    del stuck.oscillations[:-EVENTS_KEPT]
    return event


def _give_up_walks(m: Memory, w: WorldModel, moves: list[tuple[str, str, str]]) -> tuple[list[dict], list[dict]]:
    """Give up every walk on this map that made one of ``moves``; then every
    stuck attempt whose step 2 block was one of their targets leaves step 2.

    A walk with a stuck attempt of its own is given up through it; any other
    (a walk to a block, a waypoint) through a fresh one, so it is remembered
    and backed off the same way. Returns the walks given up, in the order
    they first moved, and the attempts that left step 2.
    """
    stuck = m.nav_stuck
    given: list[dict] = []
    targets: set = set()
    for key in dict.fromkeys(k for _, _, k in moves if k):
        map_id, target = nav_stuck.key_dest(key)
        if map_id != w.map_id:
            continue
        goal = next(g for g, _, k in moves if k == key)
        att = stuck.attempts.get(key) or nav_stuck.NavAttempt(key=key, goal=goal, target=target, map_id=map_id)
        nav_stuck.give_up(m, w, att, GIVE_UP_REASON)
        targets.add(target)
        walkers = sorted({state for _, state, k in moves if k == key and state})
        given.append({"goal": goal, "target": list(target), "states": walkers})
    escalated: list[dict] = []
    for att in list(stuck.attempts.values()):
        block = nav_stuck.break_target(att)
        if att.map_id == w.map_id and att.level == nav_stuck.BREAK and block in targets:
            nav_stuck.escalate(m, w, att, GIVE_UP_REASON)
            escalated.append({"goal": att.goal, "target": list(att.target), "block": list(block)})
    return given, escalated


def note_move(m: Memory, w: WorldModel, intents: list[dict] | None, state: str) -> None:
    """After a decision: which walk the move it sends belongs to.

    A move is a walk's when it lands on the next cell of ``m.path``, whoever
    planned that path; else it is a step of its own (a Fight approach, a
    detour step) and no walk's. The next ``check`` files it with the cell
    the move led to.
    """
    target = None
    for intent in intents or []:
        if intent.get("verb") == "SetPosition":
            target = (intent["x"], intent["y"])
            break
    goal = m.goal if target is not None and m.path[:1] == [target] else ""
    m.nav_stuck.last_move = (goal, state if intents else "", walk_key(m, w, goal))


def walk_key(m: Memory, w: WorldModel, goal: str) -> str:
    """The give-up key of the target the walk ``goal`` heads for, or "" for no walk.

    The target is the active stuck attempt's when that attempt is this
    walk's, else the one the walk committed to (``Memory.walks``), else
    where its path ends. Which attempt is active does not change whether a
    walk is filed: every walk has a target.
    """
    if not goal or w.map_id is None:
        return ""
    stuck = m.nav_stuck
    att = stuck.attempts.get(stuck.active) if stuck.active else None
    if att is not None and att.goal == goal and att.map_id == w.map_id:
        return att.key
    walk = m.walks.get(goal)
    if walk is not None and walk.map_id == w.map_id:
        return nav_stuck.goal_key(goal, w.map_id, walk.target)
    return nav_stuck.goal_key(goal, w.map_id, m.path[-1]) if m.path else ""


def take_escape(m: Memory, w: WorldModel) -> set[Pos]:
    """The cells survival-only pacing just ran between, other than the one we stand on; cleared once read.

    Flee and Retreat call this when they act; a non-empty answer means the
    guard caught them pacing and the escape they plan now must avoid these
    cells (module docstring).
    """
    stuck = m.nav_stuck
    cells, stuck.escape_from = stuck.escape_from, set()
    return cells - {w.pos}
