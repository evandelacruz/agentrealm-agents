"""The walker commits to its path (A15, A58 run 5).

A fresh plan every decision flips between two near-equal routes: fog shows
one tile of the other route, that route is a step cheaper for a moment, and
the walker turns round. So a walk keeps the path it chose until one of:

- it arrives (the path runs out);
- a step on it is rejected, or newly seen terrain blocks it (a cell on it
  is no longer passable on the cost grid: rejections land in ``avoid``);
- its target changes (another goal, map or cell);
- a new plan is cheaper by more than ``SWITCH_GAIN`` of the rest of it.

A new path whose first step goes back to the cell the walk just came from
is taken only when the old one is blocked, however much cheaper it looks.
Equal paths therefore never alternate: the one already walked wins.

``pathing`` runs every walk through ``commit``: Explore's goals and plan
ops (``replan``), Travel, Recover, Level, Investigate and Boss
(``guided_step``), Heal and Loot (``bounded_step``). An escalation level
that plans afresh (A15 steps 1, 3 and 4) starts a new walk with ``start``,
since the old path is the one that got stuck.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..world import Pos, WorldModel
from .planner import CostGridParams, path_cost

# A new plan replaces the path being walked only when it is cheaper by more
# than this share of the rest of that path.
SWITCH_GAIN = 0.2


@dataclass
class Walk:
    """The path a walk committed to, from the cell it was planned on."""

    goal: str
    map_id: int | None
    target: Pos
    cells: list[Pos]  # the cell it was planned on, then each step
    came_from: Pos | None = None  # the cell before ``cells[0]``, when the walk went on from another


def rest(walk: Walk | None, w: WorldModel, goal: str, target: Pos) -> tuple[list[Pos], Pos | None] | None:
    """What is left of ``walk`` from where we stand, and the cell we came from.

    None when ``walk`` is for another goal, map or target, or we are off it.
    """
    if walk is None or (walk.goal, walk.map_id, walk.target) != (goal, w.map_id, target):
        return None
    if w.pos not in walk.cells:
        return None
    i = len(walk.cells) - 1 - walk.cells[::-1].index(w.pos)
    return walk.cells[i + 1 :], walk.cells[i - 1] if i else walk.came_from


def commit(
    walk: Walk | None,
    w: WorldModel,
    goal: str,
    target: Pos,
    found: list[Pos] | None,
    params: CostGridParams,
) -> tuple[list[Pos] | None, Walk | None]:
    """The path to walk toward ``target`` (the kept one, or ``found``) and the walk to remember."""
    on = rest(walk, w, goal, target)
    came_from = None
    if on is not None:
        kept, came_from = on
        kept_cost = path_cost(w, kept, target, params) if kept else None
        if kept_cost is not None:
            found_cost = path_cost(w, found, target, params) if found else None
            back = bool(found) and found[0] == came_from
            if found_cost is None or back or found_cost >= (1 - SWITCH_GAIN) * kept_cost:
                return kept, walk
    if not found:
        # Another goal's walk is left alone; this goal's ends with no way on.
        return found, None if on is not None else walk
    return found, start(w, goal, target, found, came_from)


def start(w: WorldModel, goal: str, target: Pos, path: list[Pos], came_from: Pos | None = None) -> Walk | None:
    """A new walk along ``path``, dropping whatever was walked before."""
    if not path or w.pos is None:
        return None
    return Walk(goal, w.map_id, target, [w.pos, *path], came_from)
