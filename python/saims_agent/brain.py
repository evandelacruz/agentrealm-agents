"""Deciding: which call to spend this window on, and which intent to send.

Both are pure functions of the world model and the policy, so the same
observations give the same choice and a trace reads back as a decision.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import Policy
from .world import DOORS, Entity, Pos, WorldModel, chebyshev

SELF_REFRESH = 60  # windows between self reads when nothing forces one


# Scheduler.

@dataclass
class Memory:
    """What the brain carries between windows besides the world model."""

    path: list[Pos] = field(default_factory=list)
    goal: str = ""
    need_position: bool = True
    need_self: bool = True
    windows_since_self: int = 0
    pending: dict | None = None  # the intent submitted last, awaiting its result
    pending_queue: str | None = None  # the queue_id `pending` was sent under
    undo: Pos | None = None  # where we stood before assuming `pending` moved us
    blocked: dict[Pos, int] = field(default_factory=dict)  # rejected tile -> decisions left to keep off it
    alarm: bool = False  # Damaged or Attacked since the last entity read


def choose_call(w: WorldModel, m: Memory, policy: Policy) -> str:
    """One of: self, position, terrain, entities, tick."""
    if m.need_self or m.windows_since_self >= SELF_REFRESH:
        return "self"
    if m.need_position or w.pos is None:
        return "position"
    if policy.kind in ("idle",):
        return "tick"
    if w.terrain_map != w.map_id or w.terrain_center is None or chebyshev(w.terrain_center, w.pos) > w.perception // 2:
        return "terrain"
    if m.alarm or w.tick - w.entities_tick >= policy.entity_refresh:
        return "entities"
    return "tick"


# Intents.

def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def use_on(e: Entity) -> dict:
    return {"verb": "Use", "target": {"kind": "character", "character_id": e.id}}


def take(e: Entity) -> dict:
    return {"verb": "Take", "supply_id": e.id}


def withdraw_all(chest_id: int) -> dict:
    # No supply_ids: take everything that fits, lowest ids first (B117).
    return {"verb": "WithdrawFromChest", "chest_id": chest_id}


@dataclass
class Decision:
    intent: dict | None
    reason: str


BLOCK_WINDOWS = 1  # decisions to keep off a tile after a step into it was rejected


def reject_step(m: Memory, p: Pos) -> None:
    """Reflex 1: a rejected step clears the plan and keeps us off that tile."""
    m.path, m.goal = [], ""
    m.blocked[p] = BLOCK_WINDOWS


def decide(w: WorldModel, m: Memory, policy: Policy, rng: random.Random) -> Decision:
    """The reflex list from PLAN.md. The first rule that matches wins.

    Each call is one decision window: it ages the tiles reflex 1 blocked.
    """
    d = _decide(w, m, policy, rng)
    m.blocked = {p: n - 1 for p, n in m.blocked.items() if n > 1}
    return d


def _decide(w: WorldModel, m: Memory, policy: Policy, rng: random.Random) -> Decision:
    if policy.kind == "idle" or w.pos is None or not w.alive:
        return Decision(None, "idle")
    here = w.pos
    view = w.view
    blocked = set(m.blocked)

    if policy.kind == "wander":
        return _wander(w, m, rng)

    # 1. A rejected step is kept out of every choice below until it ages out.
    # 2. Standing on a block that hurts.
    if view.tiles.get(here) in policy.avoid_blocks:
        safe = [n for n in w.open_neighbours(here, blocked) if view.tiles.get(n) not in policy.avoid_blocks]
        if safe:
            m.path = []
            return Decision(set_position(min(safe)), f"off {view.tiles.get(here)}")

    # 3. Hostiles.
    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight":
            if target.kind == "character":
                return Decision(use_on(target), f"fight {target.kind} {target.id}")
            # NPC targets have no Use target kind on the wire yet; fall through to flee.
        away = _flee_step(w, hostiles, blocked)
        if away is not None:
            m.path = []
            return Decision(set_position(away), f"flee {target.kind} {target.id}")

    # 4. Supplies within reach.
    if policy.pickup:
        near = [e for e in w.entities if e.kind == "supply" and chebyshev(e.pos, here) <= 1]
        if near:
            s = min(near, key=lambda e: (chebyshev(e.pos, here), e.id))
            return Decision(take(s), f"take {s.code or s.id}")

    # 4b. Our death chest: go back for it and take everything out (B103).
    if policy.pickup and w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, chest_id = w.death_chest
        if chebyshev(at, here) <= 1:
            contents = w.chest_contents.get(chest_id)
            if contents:
                return Decision(withdraw_all(chest_id), f"recover from chest {chest_id}")
            if contents is None:
                return Decision(None, f"open chest {chest_id}")
        elif m.goal != "chest" or not m.path or not _step_open(w, m, m.path[0]):
            found = w.path(at, avoid=blocked)
            if found:
                m.path, m.goal = found, "chest"

    # 5. Follow the plan, replanning when it is empty or its next step is not open.
    if not m.path or not _step_open(w, m, m.path[0]):
        _replan(w, m, policy, rng)
    if m.path:
        return Decision(set_position(m.path[0]), f"{m.goal} → {m.path[-1]}")

    # 6. Nothing to do.
    return Decision(None, "no goal reachable")


def _wander(w: WorldModel, m: Memory, rng: random.Random) -> Decision:
    options = w.open_neighbours(w.pos, set(m.blocked))
    if not options:
        return Decision(None, "wander: boxed in")
    return Decision(set_position(rng.choice(sorted(options))), "wander")


def _flee_step(w: WorldModel, hostiles: list[Entity], blocked: set[Pos]) -> Pos | None:
    here = w.pos
    options = w.open_neighbours(here, blocked) + [here]

    def safety(p: Pos) -> tuple[int, int]:
        nearest = min(chebyshev(p, h.pos) for h in hostiles)
        total = sum(chebyshev(p, h.pos) for h in hostiles)
        return nearest, total

    best = max(options, key=lambda p: (safety(p), p))
    return None if best == here else best


def _step_open(w: WorldModel, m: Memory, p: Pos) -> bool:
    if chebyshev(w.pos, p) > w.movement:
        return False
    if p in m.blocked:
        return False
    if w.view.tiles.get(p) in DOORS:
        return True
    return w.view.walkable(p) and p not in w.occupied()


def _replan(w: WorldModel, m: Memory, policy: Policy, rng: random.Random) -> None:
    m.path, m.goal = [], ""
    for goal in policy.goals:
        found = _plan_goal(goal, w, policy, rng, set(m.blocked))
        if found:
            m.path, m.goal = found, goal
            return


def _plan_goal(goal: str, w: WorldModel, policy: Policy, rng: random.Random, blocked: set[Pos]) -> list[Pos] | None:
    view = w.view
    if goal == "hold":
        return None
    if goal == "wander":
        options = w.open_neighbours(w.pos, blocked)
        return [rng.choice(sorted(options))] if options else None
    if goal == "goto":
        target = tuple(policy.goto)
        return w.path(target, allow_goal_door=True, avoid=blocked) or None
    if goal == "doors":
        doors = {p for p, b in view.tiles.items() if b in DOORS}
        found = w.nearest(doors, allow_goal_door=True, avoid=blocked)
        return found[1] if found and found[1] else None
    if goal == "explore":
        targets = view.frontier() - {w.pos}
        found = w.nearest(targets, avoid=blocked)
        return found[1] if found and found[1] else None
    return None
