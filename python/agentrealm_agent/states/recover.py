"""Recover: walk to the death chest when its spot is safe (A11, A7)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import grid_params, nav_search, next_step
from ..world import NEIGHBOURS, MapView, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .intents import set_position, withdraw_all


def _map_view(w: WorldModel, map_id: int):
    if w.map_id == map_id:
        return w.view
    return w.maps.get(map_id, MapView())


def recover_approach_target(w: WorldModel, map_id: int, chest_at: Pos) -> Pos | None:
    """A standable cell from which the chest is in reach, known safe via get_zone (A7)."""
    known = w.zones.get(map_id, {})
    view = _map_view(w, map_id)
    safe = safe_tiles(w, map_id)
    if chest_at in known and known[chest_at].safe and view.walkable(chest_at):
        return chest_at
    options = [
        n
        for dx, dy in NEIGHBOURS
        if (n := (chest_at[0] + dx, chest_at[1] + dy)) in safe and view.walkable(n)
    ]
    return min(options) if options else None


def recover_spot_safe(w: WorldModel, map_id: int, chest_at: Pos) -> bool:
    return recover_approach_target(w, map_id, chest_at) is not None


def recover_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    knowledge: KnowledgeBase | None = None,
    state: str = "Recover",
) -> StateOutcome:
    """Path to the death chest and withdraw when adjacent (A11)."""
    here = w.pos
    if here is None or w.death_chest is None:
        return StateOutcome(None, "no death chest", state=state)
    map_id, at, chest_id = w.death_chest
    if map_id != w.map_id:
        return StateOutcome(None, "death chest on another map", state=state)
    target = recover_approach_target(w, map_id, at)
    if target is None:
        return StateOutcome(None, "death chest spot not safe", state=state)

    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    plan_avoid = nav_avoid | hazards
    plan_costly = nav_costly

    if chebyshev(at, here) <= 1:
        contents = w.chest_contents.get(chest_id)
        if contents:
            return StateOutcome(
                [withdraw_all(chest_id)], f"recover from chest {chest_id}", reflex=True, state=state
            )
        if contents is None:
            return StateOutcome(None, f"open chest {chest_id}", state=state)

    if m.goal != "chest" or not next_step(w, plan_avoid, m.path):
        found = cost_path(
            w,
            target,
            grid_params(policy, plan_avoid, plan_costly),
            nav=nav_search(m, w, "chest", target),
        )
        if next_step(w, plan_avoid, found):
            m.path, m.goal = found, "chest"

    step = next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"chest → {m.path[-1]}", state=state)

    return StateOutcome(None, "chest not reachable", state=state)


class RecoverState(State):
    name = "Recover"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if world.death_chest is None or world.death_chest[0] != world.map_id:
            return False
        _, at, _ = world.death_chest
        return recover_spot_safe(world, world.map_id, at)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return recover_outcome(
            world, ctx.memory, ctx.policy, knowledge=ctx.knowledge, state=self.name
        )
