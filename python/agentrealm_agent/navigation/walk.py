"""The walker commits to its path (A15, A58 run 5).

A fresh plan every decision flips between two near-equal routes: fog shows
one tile of the other route, that route is a step cheaper for a moment, and
the walker turns round. So a walk keeps the path it chose until one of:

- it arrives (the path runs out);
- a step on it is rejected, or newly seen terrain blocks it (a cell on it
  is no longer passable on the cost grid: rejections land in ``avoid``);
- its target changes (another goal, map or cell). Which target a goal
  walks to is committed separately (``targets``, A71), so a cheaper path to
  another target never replaces the walk. An explore walk's target is a
  frontier cell, and fog reveals move the frontier nearly every decision,
  so ``follow_frontier`` keeps it heading for the ground it was exploring
  instead: while its target is still a frontier cell (or unseen) the walk
  stands, and once a reveal takes that cell away the walk re-aims at the
  nearest frontier it can reach without stepping back. A reveal on its own
  never turns it round;
- a new plan to the same target is cheaper by more than ``SWITCH_GAIN`` of
  the rest of it.

A new path whose first step goes back to the cell the walk just came from
is taken only when the old one is blocked, or when the new one is cheaper
overall and the old one's hostile cost is higher by more than
``SWITCH_GAIN`` (A16 Walk run 3); never for a route that only looks cheaper. Equal
paths therefore never alternate: the one already walked wins.

Each goal keeps its own walk (``Memory.walks``), so a goal whose kept path
waits on fog keeps it while a later goal takes the move. Wherever a goal's
path is dropped for good (a give-up, a break that opened the way, a
directives reload, a backoff) its walk goes too (``drop``), so a route
given up on is never picked up again.

The kept path and the new plan are priced on the same cost grid the planner
searched (``params``); comparing them on any other grid would let a kept
path through a hazard or a hostile's reach beat the detour the planner chose.

``pathing`` runs every walk through ``commit``: Explore's goals and plan
ops (``replan``), Travel, Recover, Level, Investigate and Boss
(``guided_step``), Heal and Loot (``bounded_step``). An escalation level
that plans afresh (A15 steps 1, 3 and 4) starts a new walk with ``start``,
since the old path is the one that got stuck.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..world import Pos, WorldModel
from .planner import CostGridParams, hostile_cost, path_cost

if TYPE_CHECKING:
    from ..memory import Memory

# A new plan replaces the path being walked only when it is cheaper by more
# than this share of the rest of that path.
SWITCH_GAIN = 0.2

# How long a walk under way may hold the round on an unseen next cell before
# the round falls through to the states below (5 s at 10 ticks/s: a few
# terrain reads).
FOG_HOLD_TICKS = 50


@dataclass
class Walk:
    """The path a walk committed to, from the cell it was planned on."""

    goal: str
    map_id: int | None
    target: Pos
    cells: list[Pos]  # the cell it was planned on, then each step
    came_from: Pos | None = None  # the cell before ``cells[0]``, when the walk went on from another
    held: tuple[Pos, int] | None = None  # (cell, tick) it began holding on an unseen next cell


def rest(
    walk: Walk | None, w: WorldModel, goal: str, target: Pos | None
) -> tuple[list[Pos], Pos | None] | None:
    """What is left of ``walk`` from where we stand, and the cell we came from.

    None when ``walk`` is for another goal, map or target (any target when
    ``target`` is None), or we are off it.
    """
    if walk is None or (walk.goal, walk.map_id) != (goal, w.map_id):
        return None
    if target is not None and walk.target != target:
        return None
    if w.pos not in walk.cells:
        return None
    i = len(walk.cells) - 1 - walk.cells[::-1].index(w.pos)
    return walk.cells[i + 1 :], walk.cells[i - 1] if i else walk.came_from


def underway(walk: Walk | None, w: WorldModel) -> bool:
    """We stand on ``walk`` past the cell it was planned on: it has been walked, not just chosen."""
    return walk is not None and walk.map_id == w.map_id and w.pos in walk.cells[1:]


def hold_for_fog(walk: Walk | None, w: WorldModel) -> bool:
    """A walk under way whose next cell is unseen may hold the round, for ``FOG_HOLD_TICKS`` at one cell.

    After that the round falls through, so a cell that is never seen cannot
    pin the walk.
    """
    if not underway(walk, w):
        return False
    if walk.held is None or walk.held[0] != w.pos:
        walk.held = (w.pos, w.tick)
    return w.tick - walk.held[1] < FOG_HOLD_TICKS


def commit(
    walk: Walk | None,
    w: WorldModel,
    goal: str,
    target: Pos,
    found: list[Pos] | None,
    params: CostGridParams,
) -> tuple[list[Pos] | None, Walk | None]:
    """The path to walk (the kept one, or ``found`` toward ``target``) and ``goal``'s walk to remember.

    ``params`` must be the grid ``found`` was searched on (module docstring).
    Only a walk toward ``target`` itself is kept: which target a goal walks
    to is its commitment's to say (``targets``, A71), never a cheaper path
    to another one.
    """
    on = rest(walk, w, goal, target)
    came_from = None
    if on is not None:
        kept, came_from = on
        kept_cost = path_cost(w, kept, walk.target, params) if kept else None
        if kept_cost is not None:
            found_cost = path_cost(w, found, target, params) if found else None
            if found_cost is None:
                return kept, walk
            if found[0] == came_from:
                # Turning back loses, unless it is cheaper overall and the kept
                # path runs nearer the hostiles by more than SWITCH_GAIN.
                if found_cost >= kept_cost or hostile_cost(w, found, params) >= (1 - SWITCH_GAIN) * hostile_cost(
                    w, kept, params
                ):
                    return kept, walk
            elif found_cost >= (1 - SWITCH_GAIN) * kept_cost:
                return kept, walk
    if not found:
        return found, None
    return found, start(w, goal, target, found, came_from)


def follow_frontier(
    walk: Walk | None,
    w: WorldModel,
    goal: str,
    targets: Collection[Pos],
    ahead: Callable[[Pos | None], tuple[Pos, list[Pos]] | None],
) -> Walk | None:
    """``goal``'s explore walk, kept heading for the ground it was exploring.

    ``targets`` are the frontier cells the goal may walk to. While the walk's
    target is one of them, or still unseen, the walk stands as it is. Once a
    reveal has taken that cell off the frontier, the walk re-aims at the
    nearest frontier ``ahead`` finds without stepping back to the cell the
    walk came from. None when we are off the walk or nothing lies ahead, so
    the next plan is taken as it comes.
    """
    on = rest(walk, w, goal, None)
    if on is None:
        return None
    if walk.target in targets or walk.target not in w.view.tiles:
        return walk
    found = ahead(on[1])
    return start(w, goal, found[0], found[1], on[1]) if found and found[1] else None


def start(w: WorldModel, goal: str, target: Pos, path: list[Pos], came_from: Pos | None = None) -> Walk | None:
    """A new walk along ``path``, dropping whatever was walked before."""
    if not path or w.pos is None:
        return None
    return Walk(goal, w.map_id, target, [w.pos, *path], came_from)


def drop(m: Memory, goal: str | None = None, target: Pos | None = None) -> None:
    """Forget ``goal``'s walk (only one toward ``target``, when given), or every walk when ``goal`` is None."""
    if goal is None:
        m.walks.clear()
    elif goal in m.walks and target in (None, m.walks[goal].target):
        del m.walks[goal]
