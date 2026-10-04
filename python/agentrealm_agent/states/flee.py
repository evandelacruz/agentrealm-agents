"""Flee: open distance when a nearby hostile would win (A9)."""

from __future__ import annotations

from ..pathing import flee_step
from ..survival import hostiles_in_range, nearest_safe_goal, on_safe_tile, would_lose
from ..world import WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .intents import set_position
from .survival_nav import plan_surfaces


def _flee_step(w: WorldModel, hostiles, blocked, safes: set) -> tuple[int, int] | None:
    here = w.pos
    assert here is not None
    options = w.open_neighbours(here, blocked) + [here]

    def score(p):
        nearest = min(chebyshev(p, h.pos) for h in hostiles)
        to_safe = min((chebyshev(p, s) for s in safes), default=10**6)
        return (nearest, -to_safe, p)

    best = max(options, key=score)
    return None if best == here else best


class FleeState(State):
    name = "Flee"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if ctx.policy.on_hostile == "ignore":
            return False
        if not hostiles_in_range(world, ctx.policy):
            return False
        if on_safe_tile(world):
            return False
        return would_lose(world, ctx.policy, ctx.params)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        if on_safe_tile(world):
            return True
        if not hostiles_in_range(world, ctx.policy):
            return True
        return not would_lose(world, ctx.policy, ctx.params)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        hostiles = hostiles_in_range(w, policy)
        if not hostiles:
            return StateOutcome(None, "no hostiles", state=self.name)
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, w.pos), e.id))
        blocked, _, plan_avoid, _ = plan_surfaces(w, m, policy, ctx.knowledge)
        safes = safe_tiles(w, w.map_id) if w.map_id is not None else set()
        away = _flee_step(w, hostiles, blocked, safes) if safes else flee_step(w, hostiles, blocked)
        if away is None:
            away = flee_step(w, hostiles, plan_avoid)
        if away is not None:
            m.path, m.goal = [], ""
            return StateOutcome(
                [set_position(away)],
                f"flee {target.kind} {target.id}",
                reflex=True,
                state=self.name,
            )
        return StateOutcome(None, "nowhere to flee", state=self.name)
