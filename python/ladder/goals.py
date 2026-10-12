"""Lorraine's goal tree (A139): the tree as data, its readiness predicates, and the walk.

The tree, walked from the top every turn::

    beat the world
    └─ beat the next level            the lowest level not cleared (A145)
       ├─ hold what it requires       its entrance's need: a key, sandals, a bomb (A147)
       └─ ready for its risk          heals, weapon, armor against its expected danger
          └─ hold the gems for what's missing   from either branch, priced from known sellers (A131)
             └─ gather
                └─ stay alive         the risk budget of the goal above

Each node carries its readiness predicate over ``WorldModel`` and the
knowledge base (a ``Check``: ready or not, and why, in game terms), the op
that works it, shaped as Gary's plan ops (``targets.GoalOp``), and the
goal it serves (``subject``): the level for the nodes under it, the
gathering for "stay alive". Predicates and ops read only their node and the
walk's ``Reading``, so ``walk`` works on any tree ``tree()`` or a test
builds. Readiness is computed in ``ladder/readiness.py``; "ready for its
risk" and "stay alive" are its one risk function (``risk_budget``) on the
level and on the goal being worked.

**The walk.** From the root, the first child that is not ready is walked
into, children in order; a node all of whose children are ready is the
turn's goal. "Hold the gems" serves both branches: it is a child of "hold
what it requires" as well as of "ready for its risk", and counts what
either still needs bought. Where to gather and how safely is not a node:
it is the planner's answer at "gather" (A140).

**Blocked nodes.** A node the planner says it cannot work (A140) is marked
in ``Blocks`` with its reason and an expiry; the walk never makes it the
goal, notes it as skipped with that reason, and descends into its children,
taking the first with a node not ready under it; a ready node has nothing to
do, so it is never the goal. A node not ready whose whole way
down is blocked blocks its parent too, with its reason, so a node is the
goal only while all its children are ready. A block lapses at its expiry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentrealm_agent.entrance_needs import lying_in_sight, meets
from agentrealm_agent.level_order import target_level
from agentrealm_agent.supplies import heal_of, is_potion
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


@dataclass
class Reading:
    """What one walk reads, each part once: every node's check, and what
    each level still needs bought (``readiness.missing``)."""

    world: WorldModel
    kb: KnowledgeBase | None
    checks: dict[str, Check] = field(default_factory=dict)
    missing_for: dict[str, r.Missing] = field(default_factory=dict)

    def check(self, node: Node) -> Check:
        if node.key not in self.checks:
            self.checks[node.key] = node.check(node, self)
        return self.checks[node.key]

    def missing(self, level: Node) -> r.Missing:
        if level.key not in self.missing_for:
            self.missing_for[level.key] = r.missing(self.world, self.kb, level)
        return self.missing_for[level.key]


@dataclass(eq=False)
class Node:
    """One goal: its readiness predicate, the op that works it, the ops that
    can (what the planner may answer with, A140), and its children in the
    order the walk checks them. ``subject`` is the goal it serves, which its
    predicate and op read. ``value`` and ``foes`` are what the risk function
    prices it by (``readiness.RiskyGoal``)."""

    key: str
    title: str
    check: Callable[[Node, Reading], Check]
    op: Callable[[Node, Reading], GoalOp | None]
    ops: tuple[str, ...] = ()
    children: list[Node] = field(default_factory=list)
    subject: Node | None = None
    value: str = r.ORDINARY
    danger: Callable[[WorldModel, KnowledgeBase | None], list[Entity]] | None = None

    def foes(self, world: WorldModel, kb: KnowledgeBase | None) -> list[Entity]:
        return self.danger(world, kb) if self.danger else []

    @property
    def serves(self) -> Node:
        """The goal this node serves; one with no ``subject`` serves itself."""
        return self.subject or self


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
    """One walk: the goal, the nodes walked to it, the blocked nodes skipped, and what it read."""

    goal: Node | None
    path: list[Node]
    skipped: list[tuple[Node, Block]]
    reading: Reading

    @property
    def checks(self) -> dict[str, Check]:
        return self.reading.checks

    def op(self) -> GoalOp | None:
        return self.goal.op(self.goal, self.reading) if self.goal else None


# The predicates and ops, one per node.


def _explore(w: WorldModel) -> GoalOp:
    x, y = w.pos or (0, 0)
    return {"op": "explore_area", "x": x, "y": y, "radius": EXPLORE_ANYWHERE}


def world_beaten(node: Node, rd: Reading) -> Check:
    w = rd.world
    level = target_level(w)
    if level is None:
        return Check(True, f"all {w.level_count} levels cleared")
    of = f" of {w.level_count}" if w.level_count else ""
    return Check(False, f"level {level}{of} not cleared")


def no_op(node: Node, rd: Reading) -> None:
    return None


def next_level_beaten(node: Node, rd: Reading) -> Check:
    """Never ready while it is the target: beating it moves the target on."""
    level = target_level(rd.world)
    entrance = r.next_entrance(rd.world, rd.kb)
    at = f"entrance {entrance[0]}:{entrance[1][0]},{entrance[1][1]}" if entrance else "no entrance known"
    return Check(False, f"level {level}: {at}")


def enter_next_level(node: Node, rd: Reading) -> GoalOp:
    w = rd.world
    entrance = r.next_entrance(w, rd.kb)
    if entrance is None:
        return _explore(w)
    map_id, (x, y) = entrance
    if map_id != w.map_id:
        return {"op": "travel", "to": "entrance", "x": x, "y": y, "map_id": map_id}
    return {"op": "enter_level", "x": x, "y": y}


def holds_what_it_requires(node: Node, rd: Reading) -> Check:
    need = r.need_of_next_level(rd.world, rd.kb)
    if need is None:
        return Check(True, "no unmet need recorded on its entrance")
    return Check(False, f"its entrance needs {need}")


def get_what_it_requires(node: Node, rd: Reading) -> GoalOp | None:
    w = rd.world
    need = r.need_of_next_level(w, rd.kb)
    if need is None:
        return None
    lying = lying_in_sight(w, need)
    if lying is not None:
        return {"op": "fetch_item", "code": lying.code, "x": lying.pos[0], "y": lying.pos[1]}
    buy = next((p for p in rd.missing(node.serves).buys if meets(p.code, need)), None)
    return {"op": "buy", "code": buy.code} if buy else _explore(w)


def ready_for_its_risk(node: Node, rd: Reading) -> Check:
    goal = node.serves
    b = r.risk_budget(goal, rd.world, rd.kb)
    return Check(b.ok, f"{b.why()} against {r.foes_line(goal.foes(rd.world, rd.kb))}")


def get_ready_for_its_risk(node: Node, rd: Reading) -> GoalOp:
    """Arm and wear the best gear owned first, then buy what closes the risk,
    else look for a seller of it."""
    w = rd.world
    now, best = r.Loadout.now(w), r.best_owned(w, rd.kb)
    if (best.weapon, best.armor) != (now.weapon, now.armor):
        return {"op": "equip"}
    buy = next((p for p in rd.missing(node.serves).buys if p.why == r.RISK_WHY), None)
    return {"op": "buy", "code": buy.code} if buy else _explore(w)


def holds_the_gems(node: Node, rd: Reading) -> Check:
    m = rd.missing(node.serves)
    gems = rd.world.gems or 0
    priced = ", ".join(f"{p.code} {p.price}" for p in m.buys) or "nothing to buy"
    unpriced = f"; no seller known for {', '.join(m.unpriced)}" if m.unpriced else ""
    return Check(gems >= m.gems, f"{gems} of {m.gems} gems: {priced}{unpriced}")


def gather_for_it(node: Node, rd: Reading) -> GoalOp | None:
    """Gather up to the gems what is missing costs (``count`` is the total to
    hold); with none short there is nothing to gather."""
    need = rd.missing(node.serves).gems
    return {"op": "gather_gems", "count": need} if need > (rd.world.gems or 0) else None


def gathered(node: Node, rd: Reading) -> Check:
    """Gathering is never done: it is reached only while gems are short."""
    return Check(False, "gems are earned by gathering")


def alive_margin(node: Node, rd: Reading) -> Check:
    """The goal being worked priced now: only the heals that can be used now count."""
    goal = node.serves
    b = r.risk_budget(goal, rd.world, rd.kb, now=True)
    return Check(b.ok, f"{b.why()} against {r.foes_line(goal.foes(rd.world, rd.kb))}")


def restore_margin(node: Node, rd: Reading) -> GoalOp:
    """Restore the margin with the first move that makes it fit: eat food in
    sight, buy a potion; else retreat to town, out of the hostiles' reach.
    A carried heal is never drunk for it: it counted already, so a drink
    leaves the margin as it was."""
    w, goal = rd.world, node.serves
    food = r.food_in_sight(w)
    if food is not None and r.margin_after(goal, w, rd.kb, eat=heal_of(food.code)).ok:
        return {"op": "fetch_item", "code": food.code, "x": food.pos[0], "y": food.pos[1]}
    for price, code in sorted((p, c) for c, p in r.for_sale(w, rd.kb).items() if is_potion(c)):
        if price <= (w.gems or 0) and r.margin_after(goal, w, rd.kb, carry=heal_of(code)).ok:
            return {"op": "buy", "code": code}
    return {"op": "travel", "to": "town"}


def tree() -> Node:
    """Evan's tree. "Hold the gems" is the one node under both branches of the next level."""
    root = Node("world", "beat the world", world_beaten, no_op)
    level = Node(
        "level",
        "beat the next level",
        next_level_beaten,
        enter_next_level,
        ops=("enter_level", "travel", "explore_area", "fight_boss", "fight"),
        value=r.VALUABLE,
        danger=r.level_foes,
    )
    requires = Node(
        "requires",
        "hold what it requires",
        holds_what_it_requires,
        get_what_it_requires,
        ops=("buy", "fetch_item", "explore_area", "travel"),
        subject=level,
    )
    risk = Node(
        "risk",
        "ready for its risk",
        ready_for_its_risk,
        get_ready_for_its_risk,
        ops=("equip", "buy", "explore_area", "travel"),
        subject=level,
    )
    gems = Node("gems", "hold the gems for what's missing", holds_the_gems, gather_for_it, ops=("gather_gems",), subject=level)
    gather = Node("gather", "gather", gathered, gather_for_it, ops=("gather_gems",), subject=level, danger=r.foes_here)
    alive = Node(
        "alive", "stay alive", alive_margin, restore_margin, ops=("fetch_item", "buy", "travel"), subject=gather
    )
    root.children = [level]
    level.children = [requires, risk]
    requires.children = [gems]
    risk.children = [gems]
    gems.children = [gather]
    gather.children = [alive]
    return root


def find(key: str, root: Node) -> Node:
    """The node of ``key`` in the tree under ``root``."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.key == key:
            return n
        stack.extend(n.children)
    raise KeyError(key)


def walk(world: WorldModel, kb: KnowledgeBase | None, blocks: Blocks | None = None, root: Node | None = None) -> Turn:
    """The turn's goal: from the root, into the first child not ready, until a
    node whose children are all ready; only such a node is ever the goal.

    A node marked blocked is skipped with its reason and the walk descends
    into its children, all ready, taking the first with a node not ready
    under it: a ready node has nothing to do and is never the goal. A
    child not ready whose whole way down is blocked blocks its parent too,
    with the child's reason: the walk tries the parent's next child not
    ready, and with none the parent gives no goal. ``goal`` is None when the
    world is beaten or every way down is blocked. ``root`` defaults to a
    fresh ``tree()``."""
    root = root if root is not None else tree()
    blocks = blocks if blocks is not None else Blocks()
    rd = Reading(world, kb)
    skipped: list[tuple[Node, Block]] = []

    def down(n: Node, path: list[Node]) -> tuple[Node | None, list[Node], Block | None]:
        """The goal under ``n``, the path to it, and why there is none."""
        path = [*path, n]
        under: Block | None = None
        for c in n.children:
            if rd.check(c).ready:
                continue
            goal, to, why = down(c, path)
            if goal is not None:
                return goal, to, None
            if under is None and why is not None:
                under = Block(f"{c.title}: {why.reason}", why.until)
        if under is not None:
            skipped.append((n, under))
            return None, path, under
        block = blocks.active(n.key, world.tick)
        if block is None:
            # A ready node has nothing to do, whatever block above sent the walk here.
            working = not rd.check(n).ready and n.op(n, rd) is not None
            return (n, path, None) if working else (None, path, None)
        skipped.append((n, block))
        for c in n.children:
            goal, to, _ = down(c, path)
            if goal is not None:
                return goal, to, None
        return None, path, block

    if rd.check(root).ready:
        return Turn(None, [root], skipped, rd)
    goal, path, _ = down(root, [])
    return Turn(goal, path if goal else [root], skipped, rd)


def dump(
    world: WorldModel, kb: KnowledgeBase | None, blocks: Blocks | None = None, root: Node | None = None, *, goal: bool = True
) -> list[str]:
    """The tree's readiness, one line a node, then (``goal``) the turn's goal
    and its op. It reads what the walk read; a node under both branches is
    shown once."""
    root = root if root is not None else tree()
    turn = walk(world, kb, blocks, root)
    blocked = {n.key: b for n, b in turn.skipped}
    lines: list[str] = []
    shown: set[str] = set()

    def line(n: Node, depth: int) -> None:
        if n.key in shown:
            lines.append(f"{'  ' * depth}{n.title}: as above")
            return
        shown.add(n.key)
        c = turn.reading.check(n)
        mark = "ready" if c.ready else "not ready"
        if n.key in blocked:
            mark += f", blocked: {blocked[n.key].reason}"
        lines.append(f"{'  ' * depth}{n.title}: {mark} ({c.why})")
        for child in n.children:
            line(child, depth + 1)

    line(root, 0)
    if goal:
        op: GoalOp | None = turn.op()
        title = turn.goal.title if turn.goal else "none"
        lines.append(f"goal: {title}" + (f" op={json.dumps(op, sort_keys=True)}" if op else ""))
    return lines
