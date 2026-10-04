"""Recover: walk to the death chest when its spot is safe (A11, A7)."""

from __future__ import annotations

from ..config import Policy
from ..memory import Memory
from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step
from ..world import NEIGHBOURS, MapView, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome, scripted_outcome
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
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
    *,
    state: str = "Recover",
) -> StateOutcome | None:
    """Path to the death chest and withdraw when adjacent (A11, reflex 4b).

    Only the destination is checked for safety; the route is not (A9's).
    None when no step toward the chest can be planned, so the caller yields.
    """
    here = w.pos
    if here is None or w.death_chest is None:
        return None
    map_id, at, chest_id = w.death_chest
    if map_id != w.map_id:
        return None
    target = recover_approach_target(w, map_id, at)
    if target is None:
        return None

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

    step = next_step(w, plan_avoid, m.path) if m.goal == "chest" else None
    if step is not None:
        return StateOutcome([set_position(step)], f"chest → {m.path[-1]}", state=state)
    return None


class RecoverState(State):
    """Priority 3: above Explore. Reflexes 2–4 still run first (PLAN.md)."""

    name = "Recover"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        policy = ctx.policy
        if policy.kind != "scripted" or not policy.pickup or not world.alive or world.pos is None:
            return False
        if world.death_chest is None or world.death_chest[0] != world.map_id:
            return False
        _, at, _ = world.death_chest
        return recover_spot_safe(world, world.map_id, at)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        blocked, plan_avoid, plan_costly = plan_sets(world, m, policy, ctx.knowledge)
        reflex = reflex_outcome(world, m, policy, blocked, never_attack=ctx.never_attack, state=self.name)
        if reflex is not None:
            return reflex
        out = recover_outcome(world, m, policy, plan_avoid, plan_costly, state=self.name)
        if out is not None:
            return out
        # No step toward the chest: fall back to Explore's goals this round.
        fallback = scripted_outcome(
            world, m, policy, ctx.rng, never_attack=ctx.never_attack, knowledge=ctx.knowledge, plan=ctx.plan, state=self.name
        )
        fallback.reason = f"chest not reachable; {fallback.reason}"
        return fallback
