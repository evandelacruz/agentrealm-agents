"""Escape: step off ``avoid_blocks`` ground (priority 1, A9).

Runs while standing on a block type listed in ``policy.avoid_blocks``; sends
one reflex ``SetPosition`` to a safe neighbour or replans toward safety.
"""

from __future__ import annotations

from ..pathing import next_step, replan
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .intents import set_position


class EscapeState(State):
    """Priority 1. Active on damaging or forbidden ground until we step off."""

    name = "Escape"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        block = world.view.tiles.get(world.pos, "")
        return block in ctx.policy.avoid_blocks

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        if world.pos is None:
            return True
        return world.view.tiles.get(world.pos, "") not in ctx.policy.avoid_blocks

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        here = w.pos
        assert here is not None
        blocked, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path, m.goal = [], ""
            p = min(safe)
            return StateOutcome(
                [set_position(p)],
                f"off {w.view.tiles.get(here)}",
                reflex=True,
                state=self.name,
            )
        # Surrounded: cross as little hazard as the cost grid allows (plan_sets prices hazards instead of blocking them).
        step = next_step(w, plan_avoid, m.path)
        if step is None:
            replan(w, m, policy, plan_avoid, plan_costly, ctx.knowledge)
            step = next_step(w, plan_avoid, m.path)
        if step is not None:
            return StateOutcome(
                [set_position(step)],
                f"{m.goal} → {m.path[-1]}",
                reflex=True,
                state=self.name,
            )
        return StateOutcome(None, "no escape route", state=self.name)
