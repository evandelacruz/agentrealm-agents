"""Pathing helpers the states share: flee, next step, replan (M3, A12)."""

from __future__ import annotations

import random

from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation import CostGridParams, doors_goal_path, known_prefix, nearest_target, route_first_leg
from .world import DOORS, Entity, Pos, WorldModel, chebyshev


def flee_step(w: WorldModel, hostiles: list[Entity], blocked: set[Pos]) -> Pos | None:
    here = w.pos
    options = w.open_neighbours(here, blocked) + [here]

    def safety(p: Pos) -> tuple[int, int]:
        nearest = min(chebyshev(p, h.pos) for h in hostiles)
        total = sum(chebyshev(p, h.pos) for h in hostiles)
        return nearest, total

    best = max(options, key=lambda p: (safety(p), p))
    return None if best == here else best


def step_open(w: WorldModel, blocked: set[Pos], p: Pos) -> bool:
    if chebyshev(w.pos, p) > w.movement:
        return False
    if p in blocked:
        return False
    if w.view.tiles.get(p) in DOORS:
        return True
    return w.view.walkable(p) and p not in w.occupied()


def next_step(w: WorldModel, blocked: set[Pos], path: list[Pos] | None) -> Pos | None:
    """The path's first step when it is seen and open, else None."""
    prefix = known_prefix(path or [], w.view)
    if prefix and step_open(w, blocked, prefix[0]):
        return prefix[0]
    return None


def replan(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
) -> None:
    """Take the first goal whose path starts on a seen, open step.

    A path whose first step lies in fog is skipped like an unreachable goal,
    so a later goal (explore, say) gets the move while terrain reads catch up.
    """
    m.path, m.goal = [], ""
    for goal in policy.goals:
        found = plan_goal(goal, w, policy, rng, blocked, costly, knowledge)
        if next_step(w, blocked, found):
            m.path, m.goal = found, goal
            return


def grid_params(policy: Policy, avoid: set[Pos], costly: set[Pos], allow_goal_door: bool = False) -> CostGridParams:
    return CostGridParams(
        avoid=set(avoid),
        costly=set(costly),
        hostile_kinds=frozenset(policy.hostile),
        allow_goal_door=allow_goal_door,
    )


def plan_goal(
    goal: str,
    w: WorldModel,
    policy: Policy,
    rng: random.Random,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
) -> list[Pos] | None:
    view = w.view
    if goal == "hold":
        return None
    if goal == "wander":
        options = w.open_neighbours(w.pos, blocked)
        return [rng.choice(sorted(options))] if options else None
    if goal == "goto":
        # config.load guarantees goto is set when the goal is listed.
        dest_map = policy.goto_map if policy.goto_map is not None else w.map_id
        params = grid_params(policy, blocked, costly, allow_goal_door=True)
        return route_first_leg(w, knowledge, dest_map, tuple(policy.goto), params) or None
    if goal == "doors":
        return doors_goal_path(w, knowledge, grid_params(policy, blocked, costly, allow_goal_door=True))
    if goal == "explore":
        targets = view.frontier() - {w.pos}
        found = nearest_target(w, targets, grid_params(policy, blocked, costly))
        return found[1] if found and found[1] else None
    return None
