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

Another candidate becoming nearer is never a reason. A prerequisite that
comes up on the way (a key before a locked entrance) is a *stop* inserted
before the target, never a new target: the target stays committed while the
stop is walked, and the walk resumes toward it after. A short detour to a
valuable in view (``states/detour.py``) needs no stop: it is a reflex with
its own record (``Memory.detour``), and the walk it interrupted resumes
because its target is still committed here.

Each committing state keeps one ``Commitment`` in ``Memory.targets`` under
its own key (not always its ``Memory.goal`` label):

| Key | State | Target |
|---|---|---|
| ``explore``, ``explore_area`` | Explore (safe default, plan op) | (map, frontier cell) |
| ``safe`` | Retreat and Park (``pathing.RETREAT_TARGET``) | (map, safe cell) |
| ``heal_safe`` | Heal's rest and measure walks (``pathing.HEAL_TARGET``) | (map, safe cell) |
| ``heal_explore``, ``heal_food`` | Heal | (map, zone edge cell); food supply id |
| ``travel`` | Travel, symbolic ``to`` (``states.travel.TRAVEL_TARGET``) | ``ResolvedDestination`` |
| ``loot`` | Loot | (supply id, cell) |
| ``level`` | Level inside a level (``LEVEL_TARGET``) | (goal label, map, cell) |
| ``level:entrance`` | Level, with its prerequisite stops | (map, entrance cell) |

Gather keeps its own record, ``Memory.gather_target``, under the same rules.

A state asks ``hold`` for its target: the committed one while the state's
own ``keep`` test passes, else its ``pick``, committed from then on.
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
# Decisions in a row with no path to the target, though not proven walled in,
# before it is let go: a backstop for a search that can neither find nor rule out a way.
MISS_LIMIT = 10


@dataclass
class Stop:
    """A cell to visit before the committed target: a prerequisite or a detour."""

    pos: Pos
    why: str  # "prerequisite: key", "detour: gem"
    supply_id: int | None = None  # the supply to take there, when there is one
    tries: int = 0  # Takes sent for it; a stop that keeps failing is given up


@dataclass
class Commitment:
    """The target one walk label committed to, and the plan op it serves."""

    goal: str
    op: dict | None  # the plan op the target serves; None for a reflex or the safe default
    target: Hashable  # a cell, or whatever the state resolves (Travel: a destination)
    since: int  # tick it was committed
    seen: int  # tick a state last pursued it; ``LAPSE_TICKS`` after, it lapses
    misses: int = 0  # decisions in a row it had no path, not yet proven impossible
    given_up: set = field(default_factory=set)  # cells of stops given up on: never inserted again for this target
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


def missed(m: Memory, goal: str) -> bool:
    """``goal`` had no path to its target this decision: True once that has
    happened ``MISS_LIMIT`` times in a row, and the target is released."""
    c = m.targets.get(goal)
    if c is None:
        return True
    c.misses += 1
    if c.misses < MISS_LIMIT:
        return False
    del m.targets[goal]
    return True


def reached_way(m: Memory, goal: str) -> None:
    """``goal`` has a path to its target again: the misses start over."""
    c = m.targets.get(goal)
    if c is not None:
        c.misses = 0


def add_stop(m: Memory, goal: str, stop: Stop) -> bool:
    """Insert ``stop`` before ``goal``'s target, ahead of any stop already there.

    The target stays committed. False when ``goal`` has no commitment,
    already stops at that cell, or gave a stop there up.
    """
    c = m.targets.get(goal)
    if c is None or stop.pos in c.given_up or any(s.pos == stop.pos for s in c.stops):
        return False
    c.stops.insert(0, stop)
    return True


def next_stop(m: Memory, goal: str) -> Stop | None:
    """The first stop still ahead of ``goal``'s target, or None."""
    c = m.targets.get(goal)
    return c.stops[0] if c is not None and c.stops else None


def finish_stop(m: Memory, goal: str, stop: Stop, *, given_up: bool = False) -> None:
    """``stop`` was visited, or can no longer be: the walk resumes toward the target.

    ``given_up``: it could not be done (no room, refused, no way there), so
    it is not inserted again while this target stays committed.
    """
    c = m.targets.get(goal)
    if c is not None and stop in c.stops:
        c.stops.remove(stop)
        if given_up:
            c.given_up.add(stop.pos)
