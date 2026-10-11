"""Openings: the blocks in view that stand between reached ground and the rest (A114).

When every cell a walk reaches is seen and the goal still lies beyond, the
way on is to open one: a breakable block on the edge of the ground the
character can walk to, with something past it that walk does not reach
(unseen cells, or walkable ground or a door cut off from here). Which of them a held item can open is the one break rule
(``break_memory``: the block type's documented openers, or one that opened
that cell before, never one that already failed there, never a block whose
walk was given up). Each is priced the way every walk target is: the steps
to stand within the opener's reach, plus the break's own cost
(``break_memory.break_step_cost``: the weapon's cooldown, and the gem price
of a tool the break uses up).

Explore opens the cheapest when it has nothing left to explore, and State
lists them (``ways_to_open``) so the planner can weigh a clue and pick
another with a ``break_block`` op.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Mapping

from .break_memory import BreakChoice, breakable_block, choice_at, held_capabilities, held_supplies, known_openers
from .knowledge_base import KnowledgeBase
from .reach import NEAR, supply_reach
from .world import DOORS, NEIGHBOURS, Pos, WorldModel

# Cells the flood over reached ground looks at, at most: a big overworld's
# known ground is far larger than any walk an opening is worth.
REACH_NODE_BUDGET = 20_000


@dataclass(frozen=True)
class Opening:
    """A block that opens a way: where it is, what it is, how it opens, and its price.

    ``choice`` is None when nothing held is known to open it (``opened_by``
    names what would). ``walk`` is the price of the walk to stand within
    reach of it; ``beyond`` counts the cells past it the walk does not reach.
    """

    pos: Pos
    block: str
    choice: BreakChoice | None
    opened_by: frozenset[str]
    walk: int
    beyond: int

    @property
    def price(self) -> int | None:
        """The walk plus the break, or None when nothing held opens it."""
        return None if self.choice is None else self.walk + self.choice.cost


def reached_ground(
    w: WorldModel, blocked: set[Pos], priced: Mapping[Pos, int] | None = None
) -> dict[Pos, int]:
    """Every known walkable cell a walk from here reaches, with its walk's
    price: one per step plus what priced ground adds (as Gather prices a walk).

    Ground in ``blocked`` (Step rejections, hazards) is not walked."""
    if w.pos is None:
        return {}
    priced = priced or {}
    best: dict[Pos, int] = {w.pos: 0}
    queue: list[tuple[int, Pos]] = [(0, w.pos)]
    while queue and len(best) < REACH_NODE_BUDGET:
        cost, p = heapq.heappop(queue)
        if cost > best.get(p, cost):
            continue
        for dx, dy in NEIGHBOURS:
            n = (p[0] + dx, p[1] + dy)
            if n in blocked or not w.view.walkable(n):
                continue
            c = cost + 1 + priced.get(n, 0)
            if c < best.get(n, c + 1):
                best[n] = c
                heapq.heappush(queue, (c, n))
    return best


def _beyond(w: WorldModel, pos: Pos, reached: Mapping[Pos, int]) -> int:
    """The cells next to ``pos`` the walk does not reach and that lead on:
    unseen, walkable or a door. Void (no ground, as past a map's edge) and
    solid blocks lead nowhere."""
    count = 0
    for dx, dy in NEIGHBOURS:
        n = (pos[0] + dx, pos[1] + dy)
        if n in reached:
            continue
        block = w.view.tiles.get(n)
        if block is None or w.view.walkable(n) or block in DOORS:
            count += 1
    return count


def _walk_to(pos: Pos, reach: int, reached: Mapping[Pos, int]) -> int | None:
    """The cheapest reached cell within ``reach`` of ``pos``, by its walk's price."""
    stands = [
        reached[(pos[0] + dx, pos[1] + dy)]
        for dx in range(-reach, reach + 1)
        for dy in range(-reach, reach + 1)
        if (pos[0] + dx, pos[1] + dy) in reached
    ]
    return min(stands) if stands else None


def openings(
    w: WorldModel,
    kb: KnowledgeBase | None,
    blocked: set[Pos],
    skip: frozenset[Pos] = frozenset(),
    priced: Mapping[Pos, int] | None = None,
) -> list[Opening]:
    """Every breakable in view on the edge of reached ground with something
    past it, cheapest first: the ones a held item opens by price, then the
    ones nothing held opens by their walk. A block in ``skip`` (its walk
    given up, ``break_memory.given_up_blocks``) is left out."""
    if w.pos is None or w.map_id is None:
        return []
    reached = reached_ground(w, blocked, priced)
    held = held_capabilities(w, kb)
    # Blocks are looked for only within the longest reach held of reached ground.
    far = max([NEAR, *(supply_reach(kb, s.code) for s in held_supplies(w))])
    edge = {(p[0] + dx, p[1] + dy) for p in reached for dx in range(-far, far + 1) for dy in range(-far, far + 1)}
    found: list[Opening] = []
    for pos in sorted(edge - skip):
        block = breakable_block(w, pos)
        if block is None:
            continue
        choice = choice_at(w, kb, pos, held, skip)
        reach = supply_reach(kb, choice.supply.code) if choice is not None else NEAR
        walk = _walk_to(pos, reach, reached)
        if walk is None:
            continue
        beyond = _beyond(w, pos, reached)
        if beyond:
            found.append(Opening(pos, block, choice, known_openers(w, kb, pos), walk, beyond))
    found.sort(key=lambda o: (o.price is None, o.price if o.price is not None else o.walk, -o.beyond, o.pos))
    return found


def cheapest_opening(
    w: WorldModel,
    kb: KnowledgeBase | None,
    blocked: set[Pos],
    skip: frozenset[Pos] = frozenset(),
    priced: Mapping[Pos, int] | None = None,
    kept: Pos | None = None,
) -> Opening | None:
    """The opening to work on: ``kept`` while a held item still opens it
    (a target is kept until opened, failed or given up, A71), else the
    cheapest a held item opens; None when nothing held opens any."""
    found = [o for o in openings(w, kb, blocked, skip, priced) if o.choice is not None]
    return next((o for o in found if o.pos == kept), found[0] if found else None)
