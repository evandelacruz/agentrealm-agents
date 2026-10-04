"""Investigate: reads, speech, and door or entrance looks (A30)."""

from __future__ import annotations

from ..door_look import LOOK_GOAL, apply_door_look, approach_pos, ready_to_look
from ..interest_list import pick_interest_tick
from ..navigation import cost_path, route_first_leg
from ..pathing import grid_params, guided_step, nav_search
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .intents import read_block, say_to, set_position


class InvestigateState(State):
    name = "Investigate"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        item = pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory)
        if item is None:
            return StateOutcome(None, "nothing to investigate", state=self.name)
        if item.kind == "read_block" and item.map_id is not None and item.pos is not None:
            return StateOutcome([read_block(item.map_id, item.pos)], item.reason, state=self.name)
        if item.kind == "say" and item.npc is not None:
            return StateOutcome([say_to(item.npc)], item.reason, state=self.name)
        if item.kind == "look_door" and item.map_id is not None and item.pos is not None:
            return _look_outcome(world, ctx, item.map_id, item.pos, item.reason, item.key)
        return StateOutcome(None, item.reason, state=self.name)


def _look_outcome(
    w: WorldModel,
    ctx: PlayContext,
    map_id: int,
    door_pos: tuple[int, int],
    reason: str,
    reject_key: str,
) -> StateOutcome:
    kb = ctx.knowledge
    if ready_to_look(w, map_id, door_pos):
        if apply_door_look(kb, w, map_id, door_pos):
            ctx.memory.path, ctx.memory.goal = [], ""
            return StateOutcome(None, f"looked {door_pos}", state=InvestigateState.name)
        return StateOutcome(None, "door not revealed yet", state=InvestigateState.name)
    _, plan_avoid, plan_costly = plan_sets(w, ctx.memory, ctx.policy, kb)
    stand = approach_pos(w, kb, map_id, door_pos)

    def plan(att):
        params = grid_params(ctx.policy, plan_avoid, plan_costly, allow_goal_door=False, m=ctx.memory)
        if w.map_id == map_id:
            return cost_path(w, stand, params, nav=nav_search(ctx.memory, w, LOOK_GOAL, stand))
        return route_first_leg(w, kb, map_id, stand, params, nav=nav_search(ctx.memory, w, LOOK_GOAL, stand))

    step = guided_step(ctx.memory, w, LOOK_GOAL, stand, plan_avoid, plan)
    if step is not None:
        return StateOutcome([set_position(step)], reason, state=InvestigateState.name)
    # No route: count a refusal so a unreachable mark does not pin Investigate forever.
    rejections = ctx.memory.investigate_rejections
    rejections[reject_key] = rejections.get(reject_key, 0) + 1
    return StateOutcome(None, f"{reason}; no path", state=InvestigateState.name)
