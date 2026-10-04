"""Retreat: withdraw to a known safe tile when health or odds demand it (A9)."""

from __future__ import annotations

from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step
from ..survival import nearest_safe_goal, on_safe_tile, should_retreat
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import set_position
from .survival_nav import plan_surfaces


class RetreatState(State):
    name = "Retreat"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if on_safe_tile(world):
            return False
        if nearest_safe_goal(world) is None:
            return False
        return should_retreat(world, ctx.policy, ctx.params)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return on_safe_tile(world) or not should_retreat(world, ctx.policy, ctx.params)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        goal = nearest_safe_goal(w)
        if goal is None:
            return StateOutcome(None, "no safe tile known", state=self.name)
        if w.pos == goal:
            return StateOutcome(None, "at safe tile", state=self.name)
        _, _, plan_avoid, plan_costly = plan_surfaces(w, m, policy, ctx.knowledge)
        params = grid_params(policy, plan_avoid, plan_costly)
        if m.goal != "safe" or not m.path or m.path[-1] != goal:
            m.path = cost_path(w, goal, params, nav=nav_search(m, w, "safe", goal)) or []
            m.goal = "safe"
        step = next_step(w, plan_avoid, m.path)
        if step is None and m.path:
            m.path = cost_path(w, goal, params, nav=nav_search(m, w, "safe", goal)) or []
            step = next_step(w, plan_avoid, m.path)
        if step is not None:
            return StateOutcome([set_position(step)], f"retreat → safe {goal}", reflex=True, state=self.name)
        return StateOutcome(None, "safe tile unreachable", state=self.name)
