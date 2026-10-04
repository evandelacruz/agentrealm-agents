"""Investigate: interest-list reads, speech, zone probes, and looks (A30)."""

from __future__ import annotations

from ..interest_list import pick_interest_tick, walk_target_for_look
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import grid_params, nav_search, next_step
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import read_block, read_supply, say_to, set_position


class InvestigateState(State):
    name = "Investigate"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory, ctx.directives) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        item = pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory, ctx.directives)
        if item is None:
            return StateOutcome(None, "nothing to investigate", state=self.name)
        m = ctx.memory
        if item.kind == "read_block" and item.map_id is not None and item.pos is not None:
            return StateOutcome([read_block(item.map_id, item.pos)], item.reason, state=self.name)
        if item.kind == "read_supply" and item.supply_id is not None:
            return StateOutcome([read_supply(item.supply_id)], item.reason, state=self.name)
        if item.kind == "say" and item.npc is not None:
            return StateOutcome([say_to(item.npc)], item.reason, state=self.name)
        if item.kind == "walk_look" and item.map_id is not None and item.pos is not None:
            return self._walk_to_look(world, ctx, item.map_id, item.pos, item.reason)
        return StateOutcome(None, item.reason, state=self.name)

    def _walk_to_look(
        self, world: WorldModel, ctx: PlayContext, map_id: int, target: tuple[int, int], reason: str
    ) -> StateOutcome:
        m = ctx.memory
        if world.map_id != map_id or world.pos is None:
            return StateOutcome(None, "look on another map", state=self.name)
        here = world.pos
        if chebyshev(here, target) <= 1:
            from ..investigation import mark_cell_looked

            mark_cell_looked(ctx.knowledge, map_id, target)
            return StateOutcome(None, f"looked {target}", state=self.name)
        nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, world.map_id, world.tick)
        hazards = {p for p, b in world.view.tiles.items() if b in ctx.policy.avoid_blocks}
        plan_avoid = nav_avoid | hazards
        plan_costly = nav_costly
        step_pos = walk_target_for_look(world, target, plan_avoid)
        if step_pos is None:
            return StateOutcome(None, f"no stand for {target}", state=self.name)
        if m.goal != f"look:{target}" or not next_step(world, plan_avoid, m.path):
            path = cost_path(
                world,
                step_pos,
                grid_params(ctx.policy, plan_avoid, plan_costly),
                nav=nav_search(m, world, f"look:{target}", step_pos),
            )
            if next_step(world, plan_avoid, path):
                m.path, m.goal = path, f"look:{target}"
        step = next_step(world, plan_avoid, m.path)
        if step is None:
            return StateOutcome(None, f"cannot reach {target}", state=self.name)
        return StateOutcome([set_position(step)], reason, state=self.name)
