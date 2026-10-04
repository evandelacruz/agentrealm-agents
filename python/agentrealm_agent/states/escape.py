"""Escape: step off damaging ground, or break out when trapped (A9, A28)."""

from __future__ import annotations

from ..break_memory import enclosing_break_choice
from ..pathing import next_step, replan
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .intents import arm, set_position, use_block


class EscapeState(State):
    name = "Escape"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        block = world.view.tiles.get(world.pos, "")
        if block in ctx.policy.avoid_blocks:
            return True
        return enclosing_break_choice(world, ctx.knowledge) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        if world.pos is None:
            return True
        block = world.view.tiles.get(world.pos, "")
        on_hazard = block in ctx.policy.avoid_blocks
        trapped = enclosing_break_choice(world, ctx.knowledge) is not None
        return not on_hazard and not trapped

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
        choice = enclosing_break_choice(w, ctx.knowledge)
        if choice is not None:
            intents: list[dict] = []
            if w.armed_code != choice.supply.code and choice.supply.id >= 0:
                if m.break_rearm is None and w.armed_code:
                    m.break_rearm = w.armed_code
                intents.append(arm(choice.supply.id))
            m.break_pending = (choice.pos, choice.capability)
            return StateOutcome(
                intents + [use_block(choice.pos)],
                f"break out {choice.capability} @ {choice.pos}",
                reflex=True,
                state=self.name,
            )
        # Surrounded: cross as little hazard as the cost grid allows (plan_sets prices hazards instead of blocking them).
        step = next_step(w, plan_avoid, m.path)
        if step is None:
            replan(w, m, policy, ctx.rng, plan_avoid, plan_costly, ctx.knowledge)
            step = next_step(w, plan_avoid, m.path)
        if step is not None:
            return StateOutcome(
                [set_position(step)],
                f"{m.goal} → {m.path[-1]}",
                reflex=True,
                state=self.name,
            )
        return StateOutcome(None, "no escape route", state=self.name)
