"""Lorraine's goal tree (A139): the tree as data, its readiness predicates, and the walk.

The tree, walked from the top every turn::

    beat the world
    └─ beat the next level            the lowest level not cleared (A145)
       ├─ hold what it requires       its entrance's need: a key, sandals, a bomb (A147)
       └─ ready for its risk          heals, weapon, armor against its expected danger
                                      (both take back what a death chest of hers holds
                                      for it ahead of buying it, A167)
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
goal and notes it as skipped with that reason. Its siblings come first, in
tree order (A169); with none to work, the walk descends into its children,
taking the first with a node not ready anywhere under it, under a ready
child too: a blocked goal never ends the tree's work, and "gather" is
never ready and always has an op, so while a gather target exists the
tree has work. A ready node has nothing to do, so it is never the goal. A
node not ready whose whole way down is blocked blocks its parent too, with
its reason, so a node is the goal only while all its children are ready. A
block lapses at its expiry, or earlier once what it may wait on has changed
(``ask.lift_changed``): the part of the node's state its reason named, the
state the planner saw (her region, what she carries, her health), or the
backoff of a walk given up for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentrealm_adk.entrance_needs import lying_in_sight, meets
from agentrealm_adk.level_order import target_level
from agentrealm_adk.sellers import sellers_of, walk_estimate
from agentrealm_adk.supplies import heal_of, is_potion
from agentrealm_adk.targets import EXPLORE_ANYWHERE, GoalOp
from agentrealm_adk.value import step_gems
from agentrealm_adk.world import Entity, Pos, WorldModel, chebyshev

from . import readiness as r

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from agentrealm_adk.knowledge_base import KnowledgeBase

# How long a planner's ``cannot`` keeps a node blocked, in ticks (5 minutes at 10 Hz).
BLOCK_TICKS = 3000
# Gems "gather" works for past what is missing, when what is above it is
# blocked or bought: more gems are never wasted (A169).
GEMS_AHEAD = 10


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
    unreachable: Collection[tuple[int, Pos]] = ()  # cells a walk gave up on: no chest there is a source (A167)
    given_up: tuple[str, ...] = ()  # her walk targets given up, as the planner is told them (A169)
    checks: dict[str, Check] = field(default_factory=dict)
    missing_for: dict[str, r.Missing] = field(default_factory=dict)

    def check(self, node: Node) -> Check:
        if node.key not in self.checks:
            self.checks[node.key] = node.check(node, self)
        return self.checks[node.key]

    def missing(self, level: Node) -> r.Missing:
        if level.key not in self.missing_for:
            self.missing_for[level.key] = r.missing(self.world, self.kb, level, self.unreachable)
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
    named: str = ""  # what it waits on: an ``ask.watch_lines`` key or ``gems``; "" holds to the expiry
    was: str = ""  # what that line read when the block was marked, or the gem count awaited
    seen: tuple = ()  # the state the planner saw when it was marked (``ask.seen``); () watches none
    retry_at: int | None = None  # the tick a walk given up for it may be walked again


class Blocks(dict[str, Block]):
    """The nodes the planner said it cannot work, by key (A140 sets them)."""

    def mark(
        self, key: str, reason: str, now: int, ticks: int = BLOCK_TICKS, named: str = "", was: str = "",
        seen: tuple = (), retry_at: int | None = None,
    ) -> None:
        self[key] = Block(reason, now + ticks, named, was, seen, retry_at)

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
    m = rd.missing(node.serves)
    chest = next((t for t in m.chests if t.why == r.NEED_WHY.format(need)), None)
    if chest is not None:
        return from_chest(chest)
    buy = next((p for p in m.buys if meets(p.code, need)), None)
    return {"op": "buy", "code": buy.code} if buy else _explore(w)


def from_chest(take: r.ChestTake) -> GoalOp:
    """Take an item back from one of her death chests (A167): ``fetch_item`` names the chest."""
    item = take.item
    return {"op": "fetch_item", "code": item.code, "x": item.pos[0], "y": item.pos[1], "chest_id": item.chest_id}


def ready_for_its_risk(node: Node, rd: Reading) -> Check:
    goal = node.serves
    b = r.risk_budget(goal, rd.world, rd.kb)
    return Check(b.ok, f"{b.why()} against {r.foes_line(goal.foes(rd.world, rd.kb))}")


def get_ready_for_its_risk(node: Node, rd: Reading) -> GoalOp:
    """Arm and wear the best gear owned first, then take back what a death
    chest of hers holds toward the risk (A167), then buy what closes it,
    else look for a seller of it."""
    w = rd.world
    now, best = r.Loadout.now(w), r.best_owned(w, rd.kb)
    if (best.weapon, best.armor) != (now.weapon, now.armor):
        return {"op": "equip"}
    m = rd.missing(node.serves)
    chest = next((t for t in m.chests if t.why == r.RISK_WHY), None)
    if chest is not None:
        return from_chest(chest)
    buy = next((p for p in m.buys if p.why == r.RISK_WHY), None)
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


def gather_more(node: Node, rd: Reading) -> GoalOp:
    """Gather up to the gems what is missing costs, or ``GEMS_AHEAD`` past
    what is held when none are short: the walk comes here then only because
    what is above is blocked, and gathering is always workable (A169)."""
    need, held = rd.missing(node.serves).gems, rd.world.gems or 0
    return {"op": "gather_gems", "count": need if need > held else held + GEMS_AHEAD}


def gathered(node: Node, rd: Reading) -> Check:
    """Gathering is never done: gems are always worth earning."""
    return Check(False, "gems are earned by gathering")


def alive_margin(node: Node, rd: Reading) -> Check:
    """The goal being worked priced now: only the heals that can be used now count."""
    goal = node.serves
    b = r.risk_budget(goal, rd.world, rd.kb, now=True)
    return Check(b.ok, f"{b.why()} against {r.foes_line(goal.foes(rd.world, rd.kb))}")


def restore_margin(node: Node, rd: Reading) -> GoalOp:
    """Restore the margin with the cheapest move that makes it fit, its walk
    counted (A170): eat food in sight, at its steps, or buy a potion, at its
    price and the steps to its seller, each step at the step price
    (``value.step_gems``, raised by danger, ``readiness.step_urgency``), so
    food near beats a potion and a potion wins once food is far; else retreat
    to town, out of the hostiles' reach. A carried heal is never drunk for
    it: it counted already, so a drink leaves the margin as it was."""
    w, goal = rd.world, node.serves
    step = step_gems(w, rd.kb, r.step_urgency(w, rd.kb, goal.value))
    options: list[tuple[float, int, GoalOp]] = []
    food = r.food_in_sight(w)
    if food is not None and r.margin_after(goal, w, rd.kb, eat=heal_of(food.code)).ok:
        steps = chebyshev(w.pos, food.pos) if w.pos is not None else 0
        options.append((_walk_gems(steps, step), 0, {"op": "fetch_item", "code": food.code, "x": food.pos[0], "y": food.pos[1]}))
    for code, price in r.for_sale(w, rd.kb).items():
        if is_potion(code) and price <= (w.gems or 0) and r.margin_after(goal, w, rd.kb, carry=heal_of(code)).ok:
            walks = [n for s in sellers_of(code, w, rd.kb) if s.in_stock and (n := walk_estimate(s, w)) is not None]
            options.append((price + _walk_gems(min(walks, default=0), step), 1, {"op": "buy", "code": code}))
    return min(options, key=lambda o: o[:2])[2] if options else {"op": "travel", "to": "town"}


def _walk_gems(steps: int, step: float) -> float:
    """``steps`` at ``step`` gems a step; none costs nothing, whatever the price."""
    return steps * step if steps else 0.0


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
    gather = Node("gather", "gather", gathered, gather_more, ops=("gather_gems",), subject=level, danger=r.foes_here)
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


def walk(
    world: WorldModel,
    kb: KnowledgeBase | None,
    blocks: Blocks | None = None,
    root: Node | None = None,
    unreachable: Collection[tuple[int, Pos]] = (),
) -> Turn:
    """The turn's goal: from the root, into the first child not ready, until a
    node whose children are all ready; only such a node is ever the goal.

    A node marked blocked is skipped with its reason. The walk first takes
    the goal it reaches without going below any block, so a blocked node's
    siblings come first in tree order (A169); with none, it descends into
    each blocked node's children, all ready, taking the first with a node
    not ready anywhere under it (``past``: a ready node is walked through):
    a ready node has nothing to do and is never the goal. A
    child not ready whose whole way down is blocked blocks its parent too,
    with the child's reason: the walk tries the parent's next child not
    ready, and with none the parent gives no goal. ``goal`` is None when the
    world is beaten or every way down is blocked. ``root`` defaults to a
    fresh ``tree()``. ``unreachable`` is the cells her walks gave up on and
    are backed off from: a death chest there is no source (A167)."""
    root = root if root is not None else tree()
    blocks = blocks if blocks is not None else Blocks()
    rd = Reading(world, kb, unreachable)
    skipped: list[tuple[Node, Block]] = []

    def down(n: Node, path: list[Node], through: bool, past: bool = False) -> tuple[Node | None, list[Node], Block | None]:
        """The goal under ``n``, the path to it, and why there is none.
        ``through``: the walk may go on below a blocked node; ``past``: it is
        below one, where a ready child is walked through too."""
        path = [*path, n]
        under: Block | None = None
        for c in n.children:
            ready = rd.check(c).ready
            if ready and not past:
                continue
            goal, to, why = down(c, path, through, past)
            if goal is not None:
                return goal, to, None
            if under is None and why is not None and not ready:
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
        for c in n.children if through else ():
            goal, to, _ = down(c, path, through, past=True)
            if goal is not None:
                return goal, to, None
        return None, path, block

    if rd.check(root).ready:
        return Turn(None, [root], skipped, rd)
    # A blocked node's siblings first, in tree order; only then the nodes below the blocks (A169).
    goal, path, _ = down(root, [], through=False)
    if goal is None:
        skipped.clear()
        goal, path, _ = down(root, [], through=True)
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
