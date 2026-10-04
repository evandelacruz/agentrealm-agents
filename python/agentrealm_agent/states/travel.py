"""Travel: walk to plan travel destinations (A27)."""

from __future__ import annotations

import random

from ..config import Policy
from ..directives import attack_forbidden
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import flee_step, grid_params, nav_search, next_step
from ..travel.ops import advance_travel_op, current_travel_op
from ..travel.resolve import ResolvedDestination, at_destination, resolve_travel
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import set_position, take, use_on, withdraw_all


class TravelState(State):
    name = "Travel"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and current_travel_op(ctx.memory) is not None
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        op = current_travel_op(ctx.memory)
        if op is None:
            return True
        dest = resolve_travel(op, world, ctx.knowledge, ctx.memory.strength)
        if dest is None:
            return True
        if at_destination(world, dest):
            advance_travel_op(ctx.memory)
            return current_travel_op(ctx.memory) is None
        return False

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = current_travel_op(ctx.memory)
        if op is None:
            return StateOutcome(None, "no travel goal", state=self.name)
        dest = resolve_travel(op, world, ctx.knowledge, ctx.memory.strength)
        if dest is None:
            return StateOutcome(None, f"travel:{op.to} unresolved", state=self.name)
        reflex = _try_reflexes(world, ctx.memory, ctx.policy, ctx.never_attack, ctx.knowledge, self.name)
        if reflex is not None:
            return reflex
        return _travel_step(world, ctx.memory, ctx.policy, dest, ctx.knowledge, self.name)


def _try_reflexes(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    never_attack: list[str],
    knowledge: KnowledgeBase | None,
    state: str,
) -> StateOutcome | None:
    """Reflexes 2–4b; None when travel pathing should run."""
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    view = w.view
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    escape: set[Pos] = set()
    if here in hazards:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path = []
            p = min(safe)
            return StateOutcome([set_position(p)], f"off {view.tiles.get(here)}", reflex=True, state=state)
        escape = hazards
    plan_avoid = blocked - escape

    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight" and target.kind == "character" and not attack_forbidden(target, never_attack):
            return StateOutcome([use_on(target)], f"fight {target.kind} {target.id}", reflex=True, state=state)
        away = flee_step(w, hostiles, blocked)
        if away is not None:
            m.path = []
            return StateOutcome([set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=state)

    if policy.pickup:
        near = [e for e in w.entities if e.kind == "supply" and chebyshev(e.pos, here) <= 1]
        if near:
            s = min(near, key=lambda e: (chebyshev(e.pos, here), e.id))
            return StateOutcome([take(s)], f"take {s.code or s.id}", reflex=True, state=state)

    if policy.pickup and w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, chest_id = w.death_chest
        if chebyshev(at, here) <= 1:
            contents = w.chest_contents.get(chest_id)
            if contents:
                return StateOutcome(
                    [withdraw_all(chest_id)], f"recover from chest {chest_id}", reflex=True, state=state
                )
            if contents is None:
                return StateOutcome(None, f"open chest {chest_id}", state=state)
    return None


def _travel_step(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    dest: ResolvedDestination,
    knowledge: KnowledgeBase | None,
    state: str,
) -> StateOutcome:
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    plan_avoid = nav_avoid | hazards
    plan_costly = nav_costly
    params = grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True)
    nav = nav_search(m, w, "travel", dest.pos) if dest.map_id == w.map_id else None
    from ..navigation import route_first_leg

    if m.goal != f"travel:{dest.label}" or not m.path:
        found = route_first_leg(w, knowledge, dest.map_id, dest.pos, params, nav=nav)
        if found and next_step(w, plan_avoid, found):
            m.path, m.goal = found, f"travel:{dest.label}"
        else:
            m.path, m.goal = [], ""
    step = next_step(w, plan_avoid, m.path)
    if step is None and m.path:
        found = route_first_leg(w, knowledge, dest.map_id, dest.pos, params, nav=nav)
        if found and next_step(w, plan_avoid, found):
            m.path = found
            step = next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"travel:{dest.label} → {dest.pos}", state=state)
    return StateOutcome(None, f"travel:{dest.label} blocked", state=state)
