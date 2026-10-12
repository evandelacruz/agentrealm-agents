"""Lorraine's goal tree (A139): the tree as data, its readiness predicates, and the walk.

The tree, walked from the top every turn::

    beat the world
    └─ beat the next level            the lowest level not cleared (A145)
       ├─ hold what it requires       its entrance's need: a key, sandals, a bomb (A147)
       └─ ready for its risk          heals, weapon, armor against its expected danger
          └─ hold the gems for what's missing   from either branch, priced from known sellers (A131)
             └─ gather
                └─ stay alive         the risk budget of the goal above

Each node has a readiness predicate over ``WorldModel`` and the knowledge
base (a ``Check``: ready or not, and why, in game terms) and the op that
works it, shaped as Gary's plan ops (``targets.GoalOp``). Readiness is
computed in ``ladder/readiness.py``; "ready for its risk" and "stay alive"
are its one risk function (``risk_budget``) on the level and on the goal
being worked.

**The walk.** From the root, the first child that is not ready is walked
into, children in order; a node all of whose children are ready is the
turn's goal. "Hold the gems" serves both branches: it is a child of "hold
what it requires" as well as of "ready for its risk", and counts what
either still needs bought. Where to gather and how safely is not a node:
it is the planner's answer at "gather" (A140).

**Blocked nodes.** A node the planner says it cannot work (A140) is marked
in ``Blocks`` with its reason and an expiry; the walk never makes it the
goal, notes it as skipped with that reason, and descends into its children
(a ready child is then worked for more: more margin, more gems). A block
lapses at its expiry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentrealm_agent.entrance_needs import meets
from agentrealm_agent.level_order import target_level
from agentrealm_agent.supplies import is_potion
from agentrealm_agent.targets import EXPLORE_ANYWHERE, GoalOp
from agentrealm_agent.world import Entity, WorldModel

from . import readiness as r

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentrealm_agent.knowledge_base import KnowledgeBase

# How long a planner's ``cannot`` keeps a node blocked, in ticks (5 minutes at 10 Hz).
BLOCK_TICKS = 3000


@dataclass(frozen=True)
class Check:
    """A node's readiness: ready or not, and why."""

    ready: bool
    why: str


@dataclass(eq=False)
class Node:
    """One goal: its readiness predicate, the op that works it, the ops that
    can (what the planner may answer with, A140), and its children in the
    order the walk checks them. ``value`` and ``foes`` are what the risk
    function prices it by (``readiness.RiskyGoal``)."""

    key: str
    title: str
    check: Callable[[WorldModel, KnowledgeBase | None], Check]
    op: Callable[[WorldModel, KnowledgeBase | None], GoalOp | None]
    ops: tuple[str, ...] = ()
    children: list[Node] = field(default_factory=list)
    value: str = r.ORDINARY
    danger: Callable[[WorldModel, KnowledgeBase | None], list[Entity]] | None = None

    def foes(self, world: WorldModel, kb: KnowledgeBase | None) -> list[Entity]:
        return self.danger(world, kb) if self.danger else []


@dataclass(frozen=True)
class Block:
    reason: str
    until: int  # the tick it lapses


class Blocks(dict[str, Block]):
    """The nodes the planner said it cannot work, by key (A140 sets them)."""

    def mark(self, key: str, reason: str, now: int, ticks: int = BLOCK_TICKS) -> None:
        self[key] = Block(reason, now + ticks)

    def active(self, key: str, now: int) -> Block | None:
        """The block on ``key`` still in force at ``now``; a lapsed one is forgotten."""
        b = self.get(key)
        if b is not None and b.until <= now:
            del self[key]
            return None
        return b

    def lift(self, key: str) -> None:
        """Lift a block early: the state its reason named changed (A140)."""
        self.pop(key, None)


@dataclass
class Turn:
    """One walk: the goal, the nodes walked to it, the blocked nodes skipped, every check read."""

    goal: Node | None
    path: list[Node]
    skipped: list[tuple[Node, Block]]
    checks: dict[str, Check]

    def op(self, world: WorldModel, kb: KnowledgeBase | None) -> GoalOp | None:
        return self.goal.op(world, kb) if self.goal else None


# The predicates and ops, one per node.


def _explore(w: WorldModel) -> GoalOp:
    x, y = w.pos or (0, 0)
    return {"op": "explore_area", "x": x, "y": y, "radius": EXPLORE_ANYWHERE}


def world_beaten(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    level = target_level(w)
    if level is None:
        return Check(True, f"all {w.level_count} levels cleared")
    of = f" of {w.level_count}" if w.level_count else ""
    return Check(False, f"level {level}{of} not cleared")


def next_level_beaten(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    """Never ready while it is the target: beating it moves the target on."""
    level = target_level(w)
    entrance = r.next_entrance(w, kb)
    at = f"entrance {entrance[0]}:{entrance[1][0]},{entrance[1][1]}" if entrance else "no entrance known"
    return Check(False, f"level {level}: {at}")


def enter_next_level(w: WorldModel, kb: KnowledgeBase | None) -> GoalOp:
    entrance = r.next_entrance(w, kb)
    if entrance is None:
        return _explore(w)
    map_id, (x, y) = entrance
    if map_id != w.map_id:
        return {"op": "travel", "to": "entrance", "x": x, "y": y, "map_id": map_id}
    return {"op": "enter_level", "x": x, "y": y}


def holds_what_it_requires(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    need = r.need_of_next_level(w, kb)
    if need is None:
        return Check(True, "no unmet need recorded on its entrance")
    return Check(False, f"its entrance needs {need}")


def get_what_it_requires(w: WorldModel, kb: KnowledgeBase | None) -> GoalOp | None:
    need = r.need_of_next_level(w, kb)
    if need is None:
        return None
    lying = [e for e in w.entities if e.kind == "supply" and e.gem_price is None and meets(e.code, need)]
    if lying:
        return {"op": "fetch_item", "code": lying[0].code, "x": lying[0].pos[0], "y": lying[0].pos[1]}
    buy = next((p for p in r.missing(w, kb, LEVEL).buys if meets(p.code, need)), None)
    return {"op": "buy", "code": buy.code} if buy else _explore(w)


def ready_for_its_risk(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    b = r.risk_budget(LEVEL, w, kb)
    return Check(b.ok, f"{b.why()} against {r.foes_line(LEVEL.foes(w, kb))}")


def get_ready_for_its_risk(w: WorldModel, kb: KnowledgeBase | None) -> GoalOp:
    """Arm and wear the best gear owned first, then buy what closes the risk,
    else look for a seller of it."""
    now, best = r.Loadout.now(w), r.best_owned(w, kb)
    if (best.weapon, best.armor) != (now.weapon, now.armor):
        return {"op": "equip"}
    buy = next((p for p in r.missing(w, kb, LEVEL).buys if p.why == "the level's risk"), None)
    return {"op": "buy", "code": buy.code} if buy else _explore(w)


def holds_the_gems(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    m = r.missing(w, kb, LEVEL)
    gems = w.gems or 0
    priced = ", ".join(f"{p.code} {p.price}" for p in m.buys) or "nothing to buy"
    unpriced = f"; no seller known for {', '.join(m.unpriced)}" if m.unpriced else ""
    return Check(gems >= m.gems, f"{gems} of {m.gems} gems: {priced}{unpriced}")


def gather_for_it(w: WorldModel, kb: KnowledgeBase | None) -> GoalOp:
    return {"op": "gather_gems", "count": max(w.gems or 0, r.missing(w, kb, LEVEL).gems)}


def gathered(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    """Gathering is never done: it is reached only while gems are short."""
    return Check(False, "gems are earned by gathering")


def alive_margin(w: WorldModel, kb: KnowledgeBase | None) -> Check:
    b = r.risk_budget(GATHER, w, kb)
    return Check(b.ok, f"{b.why()} against {r.foes_line(GATHER.foes(w, kb))}")


def restore_margin(w: WorldModel, kb: KnowledgeBase | None) -> GoalOp:
    """Restore the margin: eat food in sight, drink a heal carried while
    hurt, buy a potion, else retreat to town, out of the hostiles' reach."""
    food = r.food_in_sight(w)
    if food is not None:
        return {"op": "fetch_item", "code": food.code, "x": food.pos[0], "y": food.pos[1]}
    carried = r.held_food_or_potion(w)
    if carried is not None and (w.health or 0) < (w.max_health or 0):
        return {"op": "use_item", "code": carried}
    potions = sorted((p, c) for c, p in r.for_sale(w, kb).items() if is_potion(c) and p <= (w.gems or 0))
    if potions:
        return {"op": "buy", "code": potions[0][1]}
    return {"op": "travel", "to": "town"}


# The tree. ``GEMS`` is the one node under both branches of the next level.

ALIVE = Node("alive", "stay alive", alive_margin, restore_margin, ops=("use_item", "buy", "fetch_item", "travel"))
GATHER = Node(
    "gather", "gather", gathered, gather_for_it, ops=("gather_gems",), children=[ALIVE], danger=r.foes_here
)
GEMS = Node("gems", "hold the gems for what's missing", holds_the_gems, gather_for_it, ops=("gather_gems",), children=[GATHER])
REQUIRES = Node(
    "requires",
    "hold what it requires",
    holds_what_it_requires,
    get_what_it_requires,
    ops=("buy", "fetch_item", "explore_area", "travel"),
    children=[GEMS],
)
RISK = Node(
    "risk",
    "ready for its risk",
    ready_for_its_risk,
    get_ready_for_its_risk,
    ops=("equip", "buy", "explore_area", "travel"),
    children=[GEMS],
)
LEVEL = Node(
    "level",
    "beat the next level",
    next_level_beaten,
    enter_next_level,
    ops=("enter_level", "travel", "explore_area", "fight_boss", "fight"),
    children=[REQUIRES, RISK],
    value=r.VALUABLE,
    danger=r.level_foes,
)
ROOT = Node("world", "beat the world", world_beaten, lambda w, kb: None, children=[LEVEL])


def walk(world: WorldModel, kb: KnowledgeBase | None, blocks: Blocks | None = None, root: Node = ROOT) -> Turn:
    """The turn's goal: from the root, into the first child not ready, until a
    node whose children are all ready. A blocked node is skipped with its
    reason and the walk descends into its children; ``goal`` is None when
    the world is beaten or every way down is blocked."""
    blocks = blocks if blocks is not None else Blocks()
    checks: dict[str, Check] = {}
    skipped: list[tuple[Node, Block]] = []

    def check(n: Node) -> Check:
        if n.key not in checks:
            checks[n.key] = n.check(world, kb)
        return checks[n.key]

    def down(n: Node, path: list[Node]) -> tuple[Node | None, list[Node]]:
        path = [*path, n]
        for c in n.children:
            if not check(c).ready:
                goal, to = down(c, path)
                if goal is not None:
                    return goal, to
        block = blocks.active(n.key, world.tick)
        if block is None and n.op(world, kb) is not None:
            return n, path
        if block is not None:
            skipped.append((n, block))
            for c in n.children:
                if check(c).ready:
                    goal, to = down(c, path)
                    if goal is not None:
                        return goal, to
        return None, path

    if check(root).ready:
        return Turn(None, [root], skipped, checks)
    goal, path = down(root, [])
    return Turn(goal, path if goal else [root], skipped, checks)


def dump(world: WorldModel, kb: KnowledgeBase | None, blocks: Blocks | None = None) -> list[str]:
    """The tree's readiness, one line a node, then the turn's goal and its op:
    what ``ladder status`` prints. A node under both branches is shown once."""
    turn = walk(world, kb, blocks)
    blocked = {n.key: b for n, b in turn.skipped}
    lines: list[str] = []
    shown: set[str] = set()

    def line(n: Node, depth: int) -> None:
        if n.key in shown:
            lines.append(f"{'  ' * depth}{n.title}: as above")
            return
        shown.add(n.key)
        c = turn.checks.get(n.key) or n.check(world, kb)
        mark = "ready" if c.ready else "not ready"
        if n.key in blocked:
            mark += f", blocked: {blocked[n.key].reason}"
        lines.append(f"{'  ' * depth}{n.title}: {mark} ({c.why})")
        for child in n.children:
            line(child, depth + 1)

    line(ROOT, 0)
    op: Any = turn.op(world, kb)
    goal = turn.goal.title if turn.goal else "none"
    lines.append(f"goal: {goal}" + (f" op={json.dumps(op, sort_keys=True)}" if op else ""))
    return lines
