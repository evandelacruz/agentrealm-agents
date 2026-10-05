"""Retreat: walk to a known safe tile before the next hits could kill (A9).

Priority 1. Runs with a hostile in range, a known safe destination, and
``should_retreat`` from health and the threat table; never on a safe tile or
during a boss fight (A38).
"""

from __future__ import annotations

from ..navigation import cost_path, oscillation
from ..pathing import grid_params, nav_search, next_step
from ..survival import nearest_safe_goal, on_safe_tile, should_retreat
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .boss import boss_fight_on
from .explore import plan_sets
from .intents import set_position


class RetreatState(State):
    """Priority 1. Steps toward ``nearest_safe_goal`` while retreat conditions hold."""

    name = "Retreat"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if boss_fight_on(world, ctx.memory):
            return False  # Boss retreats out or commits (A38)
        if on_safe_tile(world):
            return False
        if nearest_safe_goal(world) is None:
            return False
        return should_retreat(world, ctx.policy, ctx.params)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        if boss_fight_on(world, ctx.memory):
            return True
        return on_safe_tile(world) or not should_retreat(world, ctx.policy, ctx.params)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        goal = nearest_safe_goal(w)
        if goal is None:
            return StateOutcome(None, "no safe tile known", state=self.name)
        if w.pos == goal:
            return StateOutcome(None, "at safe tile", state=self.name)
        _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
        # The oscillation guard caught Flee/Retreat pacing: the paced cell is
        # shut, so a path stepping onto it is replanned around it below (A15).
        plan_avoid |= oscillation.take_escape(m, w)
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
