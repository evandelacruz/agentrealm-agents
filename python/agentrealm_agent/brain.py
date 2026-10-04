"""Deciding: which call to spend this window on, and which intent to send.

Both are pure functions of the world model and the policy, so the same
observations give the same choice and a trace reads back as a decision.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import Policy
from .directives import attack_forbidden
from .poll_cadence import gate_tick_call
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
    pending: dict | None = None  # last non-queue intent submitted, awaiting its result
    pending_queue: str | None = None  # the queue_id movement or intent was sent under
    pending_intents: list[dict] | None = None  # full queue last submitted with intents
    pending_next_index: int = 0  # next intent index still awaiting a result
    held_queue: dict | None = None  # server queue {"queue_id", "next_index"} while not empty
    queue_sent_tick: int = 0  # tick the last multi-intent queue was answered at
    cancel_queue: bool = False  # send [] next tick: the held queue was planned from a stale position
    last_step_tick: int | None = None  # tick our last Step applied, to pace the next queue
    blocked: dict[Pos, int] = field(default_factory=dict)  # rejected tile -> decisions left to keep off it
    alarm: bool = False  # Damaged or Attacked since the last entity read
    last_poll_tick: int = -1  # sim tick of the last POST tick (M6 cadence)
    calm_poll_interval: int = 7  # ticks between calm polls, 4–10 after each poll
    queued_ticks: int = 0  # intents still queued after the last poll, one tick each
    hurt_last_poll: bool = False  # the last poll's events carried Damaged


def choose_call(w: WorldModel, m: Memory, policy: Policy) -> str:
    """One of: self, position, terrain, entities, tick, skip.

    skip spends nothing this window: calm, and the last poll's queue still
    covers it (poll_cadence, M6). The reads rank above tick, so a calm gap's
    spare windows go to stale terrain first, then stale entities. Urgent
    windows still take those reads, because entities come only from reads
    until snapshot deltas fold them in (M6 remaining).
    """
    if m.cancel_queue:
        return "tick"  # stop the stale queue before reading anything
    if m.need_self or m.windows_since_self >= SELF_REFRESH:
        return "self"
    if m.need_position or w.pos is None:
        return "position"
    if policy.kind in ("idle",):
        return gate_tick_call(w, m, policy)
    if w.terrain_stale():
        return "terrain"
    if m.alarm or w.tick - w.entities_tick >= policy.entity_refresh:
        return "entities"
    return gate_tick_call(w, m, policy)


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
    # Reflexes 2–4b: urgent enough to drop a queue still in flight (M6).
    reflex: bool = False


BLOCK_WINDOWS = 1  # decisions to keep off a tile after a step into it was rejected


def reject_step(m: Memory, p: Pos) -> None:
    """Reflex 1: a rejected step clears the plan and keeps us off that tile."""
    m.path, m.goal = [], ""
    m.blocked[p] = BLOCK_WINDOWS


def decide(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    *,
    never_attack: list[str] | None = None,
) -> Decision:
    """The reflex list from PLAN.md. The first rule that matches wins.

    Each call is one decision window: it ages the tiles reflex 1 blocked.
    """
    d = _decide(w, m, policy, rng, never_attack=never_attack or [])
    m.blocked = {p: n - 1 for p, n in m.blocked.items() if n > 1}
    return d


def _decide(
    w: WorldModel, m: Memory, policy: Policy, rng: random.Random, *, never_attack: list[str]
) -> Decision:
    if policy.kind == "idle" or w.pos is None or not w.alive:
        return Decision(None, "idle")
    here = w.pos
    view = w.view

    if policy.kind == "wander":
        return _wander(w, m, rng)

    # 1. A rejected step is kept out of every choice below until it ages out.
    # Blocks that hurt are kept out the same way, so no plan walks into one.
    hazards = {p for p, b in view.tiles.items() if b in policy.avoid_blocks}
    blocked = set(m.blocked) | hazards
    # Plans keep off hazards, except when standing on one with no safe step
    # off: then they may cross hazards, as few as they can, to get out.
    escape: set[Pos] = set()
    # 2. Standing on a block that hurts.
    if here in hazards:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path = []
            return Decision(set_position(min(safe)), f"off {view.tiles.get(here)}", reflex=True)
        escape = hazards
    plan_avoid = blocked - escape

    # 3. Hostiles.
    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight":
            if target.kind == "character" and not attack_forbidden(target, never_attack):
                return Decision(use_on(target), f"fight {target.kind} {target.id}", reflex=True)
            # NPC targets have no Use target kind on the wire yet; fall through to flee.
            # never_attack on characters also falls through to flee.
        away = _flee_step(w, hostiles, blocked)
        if away is not None:
            m.path = []
            return Decision(set_position(away), f"flee {target.kind} {target.id}", reflex=True)

    # 4. Supplies within reach.
    if policy.pickup:
        near = [e for e in w.entities if e.kind == "supply" and chebyshev(e.pos, here) <= 1]
        if near:
            s = min(near, key=lambda e: (chebyshev(e.pos, here), e.id))
            return Decision(take(s), f"take {s.code or s.id}", reflex=True)

    # 4b. Our death chest: go back for it and take everything out (B103).
    if policy.pickup and w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, chest_id = w.death_chest
        if chebyshev(at, here) <= 1:
            contents = w.chest_contents.get(chest_id)
            if contents:
                return Decision(withdraw_all(chest_id), f"recover from chest {chest_id}", reflex=True)
            if contents is None:
                return Decision(None, f"open chest {chest_id}")
        elif m.goal != "chest" or not m.path or not _step_open(w, plan_avoid, m.path[0]):
            found = w.path(at, avoid=plan_avoid, costly=escape)
            if found:
                m.path, m.goal = found, "chest"

    # 5. Follow the plan, replanning when it is empty or its next step is not open.
    if not m.path or not _step_open(w, plan_avoid, m.path[0]):
        _replan(w, m, policy, rng, plan_avoid, escape)
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


def _step_open(w: WorldModel, blocked: set[Pos], p: Pos) -> bool:
    if chebyshev(w.pos, p) > w.movement:
        return False
    if p in blocked:
        return False
    if w.view.tiles.get(p) in DOORS:
        return True
    return w.view.walkable(p) and p not in w.occupied()


def _replan(w: WorldModel, m: Memory, policy: Policy, rng: random.Random, blocked: set[Pos], costly: set[Pos]) -> None:
    m.path, m.goal = [], ""
    for goal in policy.goals:
        found = _plan_goal(goal, w, policy, rng, blocked, costly)
        if found:
            m.path, m.goal = found, goal
            return


def _plan_goal(
    goal: str, w: WorldModel, policy: Policy, rng: random.Random, blocked: set[Pos], costly: set[Pos]
) -> list[Pos] | None:
    view = w.view
    if goal == "hold":
        return None
    if goal == "wander":
        options = w.open_neighbours(w.pos, blocked)
        return [rng.choice(sorted(options))] if options else None
    if goal == "goto":
        target = tuple(policy.goto)
        return w.path(target, allow_goal_door=True, avoid=blocked, costly=costly) or None
    if goal == "doors":
        doors = {p for p, b in view.tiles.items() if b in DOORS}
        found = w.nearest(doors, allow_goal_door=True, avoid=blocked, costly=costly)
        return found[1] if found and found[1] else None
    if goal == "explore":
        targets = view.frontier() - {w.pos}
        found = w.nearest(targets, avoid=blocked, costly=costly)
        return found[1] if found and found[1] else None
    return None
