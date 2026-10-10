"""Commit to the target and to the goal it serves (A71).

A fresh "nearest X" every decision is how pacing starts: two candidates
trade places as the character moves or fog lifts, and each decision undoes
the one before. So once a state picks a target (a grass cell, a frontier,
a safe tile, a shop cell, a door, a supply) it keeps it until one of:

- it reaches the target, or the target is gone (the grass was cut, the
  supply taken: reached by other means);
- it proves the target impossible: truly no path, or stuck detection's
  give-up at the end of its ladder (A15), not a step that is taken this
  decision or a walk that lost its path to another state;
- the plan op it serves changes (a new head is new information);
- nothing pursued it for ``LAPSE_TICKS``: the episode it served (a
  retreat, a heal) is over, and the next one picks afresh.

Another candidate becoming nearer is never a reason. What comes up on the
way is a *stop* inserted before the target, never a new target: a
prerequisite (a key before a locked door) or a short detour to a valuable
in view (``states/detour.py``). The target stays committed while its stops
are walked, and the walk resumes toward it after them.

Each walk label (``Memory.goal`` value: ``gather``, ``explore``,
``heal_rest``, ``safe``, ``travel``, ``loot``, ``level``…) keeps one
``Commitment`` in ``Memory.targets``. A state asks ``hold`` for its target:
the committed one while the state's own ``keep`` test passes, else its
``pick``, committed from then on.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from .world import Pos, WorldModel

if TYPE_CHECKING:
    from .memory import Memory

T = TypeVar("T", bound=Hashable)

# A commitment no state asked about for this long has lapsed (30 s at 10 ticks/s).
LAPSE_TICKS = 300


@dataclass
class Stop:
    """A cell to visit before the committed target: a prerequisite or a detour."""

    pos: Pos
    why: str  # "prerequisite: key", "detour: gem"
    supply_id: int | None = None  # the supply to take there, when there is one


@dataclass
class Commitment:
    """The target one walk label committed to, and the plan op it serves."""

    goal: str
    op: dict | None  # the plan op the target serves; None for a reflex or the safe default
    target: Hashable  # a cell, or whatever the state resolves (Travel: a destination)
    since: int  # tick it was committed
    seen: int  # tick a state last pursued it; ``LAPSE_TICKS`` after, it lapses
    stops: list[Stop] = field(default_factory=list)


def _op_key(op: dict | None) -> dict | None:
    return dict(op) if op is not None else None


def committed(m: Memory, w: WorldModel, goal: str, op: dict | None = None) -> Commitment | None:
    """``goal``'s commitment while it still serves ``op`` and has not lapsed, else None.

    Asking is pursuing: it keeps the commitment from lapsing.
    """
    c = m.targets.get(goal)
    if c is not None and w.tick - c.seen > LAPSE_TICKS:
        del m.targets[goal]
        return None
    if c is None or c.op != _op_key(op):
        return None
    c.seen = w.tick
    return c


def hold(
    m: Memory,
    w: WorldModel,
    goal: str,
    pick: Callable[[], T | None],
    keep: Callable[[T], bool],
    op: dict | None = None,
) -> T | None:
    """``goal``'s target: the committed one while ``keep(target)`` holds, else ``pick()``.

    ``keep`` says the target still exists and is not proven impossible; it
    must never compare the target with other candidates. A new pick is
    committed; None from ``pick`` releases the goal.
    """
    c = committed(m, w, goal, op)
    if c is not None and keep(c.target):
        return c.target
    target = pick()
    if target is None:
        m.targets.pop(goal, None)
        return None
    commit(m, w, goal, target, op)
    return target


def commit(m: Memory, w: WorldModel, goal: str, target: Hashable, op: dict | None = None) -> None:
    """Commit ``goal`` to ``target``; the same target for the same op keeps its stops."""
    c = m.targets.get(goal)
    if c is not None and c.target == target and c.op == _op_key(op):
        c.seen = w.tick
        return
    m.targets[goal] = Commitment(goal, _op_key(op), target, w.tick, w.tick)


def release(m: Memory, goal: str, target: Hashable | None = None) -> None:
    """``goal`` reached its target or proved it impossible (only that target, when given)."""
    c = m.targets.get(goal)
    if c is not None and (target is None or c.target == target):
        del m.targets[goal]


def add_stop(m: Memory, goal: str, stop: Stop) -> bool:
    """Insert ``stop`` before ``goal``'s target, ahead of any stop already there.

    The target stays committed. False when ``goal`` has no commitment or
    already stops at that cell.
    """
    c = m.targets.get(goal)
    if c is None or any(s.pos == stop.pos for s in c.stops):
        return False
    c.stops.insert(0, stop)
    return True


def next_stop(m: Memory, goal: str) -> Stop | None:
    """The first stop still ahead of ``goal``'s target, or None."""
    c = m.targets.get(goal)
    return c.stops[0] if c is not None and c.stops else None


def finish_stop(m: Memory, goal: str, stop: Stop) -> None:
    """``stop`` was visited, or can no longer be: the walk resumes toward the target."""
    c = m.targets.get(goal)
    if c is not None and stop in c.stops:
        c.stops.remove(stop)
