"""Investigate: reads, NPC hellos, and door or entrance looks (A30).

Priority 4. Runs when ``pick_interest_tick`` names a readable cell, unseen NPC,
or minimap mark within the curiosity budget.
"""

from __future__ import annotations

from ..door_look import apply_door_look, approach_pos, ready_to_look
from ..interest_list import pick_interest_tick
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, guided_step, nav_search
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .intents import read_block, say_to, set_position
from .travel import route_step


class InvestigateState(State):
    """Priority 4. ``Read``, ``Say``, or walks to look at a door or entrance mark."""

    name = "Investigate"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory, params=ctx.params) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        item = pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory, params=ctx.params)
        if item is None:
            return StateOutcome(None, "nothing to investigate", state=self.name)
        if item.kind == "read_block" and item.map_id is not None and item.pos is not None:
            return StateOutcome([read_block(item.pos)], item.reason, state=self.name)
        if item.kind == "say" and item.npc is not None:
            return StateOutcome([say_to(item.npc)], item.reason, state=self.name)
        if item.kind == "look_door" and item.map_id is not None and item.pos is not None:
            return _look_outcome(world, ctx, item.map_id, item.pos, item.reason, item.key)
        return StateOutcome(None, item.reason, state=self.name)


def _look_outcome(
    w: WorldModel,
    ctx: PlayContext,
    map_id: int,
    pos: Pos,
    reason: str,
    reject_key: str,
) -> StateOutcome:
    kb = ctx.knowledge
    goal = reject_key
    if ready_to_look(w, map_id, pos):
        if apply_door_look(kb, w, map_id, pos):
            if ctx.memory.goal == goal:
                ctx.memory.path, ctx.memory.goal = [], ""
            return StateOutcome(None, f"looked {pos}", state=InvestigateState.name)
        _count_refusal(ctx, reject_key)
        return StateOutcome(None, "door not revealed yet", state=InvestigateState.name)
    if w.map_id == map_id:
        return _look_on_map(w, ctx, pos, reason, reject_key, goal)
    return _look_cross_map(w, ctx, map_id, pos, reason, reject_key, goal)


def _look_on_map(
    w: WorldModel,
    ctx: PlayContext,
    pos: Pos,
    reason: str,
    reject_key: str,
    goal: str,
) -> StateOutcome:
    kb = ctx.knowledge
    stand = approach_pos(w, pos)
    step = None
    if stand is not None:
        _, plan_avoid, plan_costly = plan_sets(w, ctx.memory, ctx.policy, kb)

        def plan(att):
            params = grid_params(ctx.policy, plan_avoid, plan_costly, allow_goal_door=False, m=ctx.memory)
            return cost_path(w, stand, params, nav=nav_search(ctx.memory, w, goal, stand))

        step = guided_step(ctx.memory, w, goal, stand, plan_avoid, plan, kb)
    if step is not None:
        return StateOutcome([set_position(step)], reason, state=InvestigateState.name)
    if nav_stuck.awaiting_break(ctx.memory, w, goal):
        # Not a refusal: Break (A28) opens the way this round.
        return StateOutcome(None, f"{reason}; stuck: break", state=InvestigateState.name)
    # No route: count a refusal so an unreachable mark does not pin Investigate forever.
    _count_refusal(ctx, reject_key)
    return StateOutcome(None, f"{reason}; no path", state=InvestigateState.name)


def _look_cross_map(
    w: WorldModel,
    ctx: PlayContext,
    map_id: int,
    pos: Pos,
    reason: str,
    reject_key: str,
    goal: str,
) -> StateOutcome:
    """Walk toward a look on another map through known door warps, as Travel does."""
    m = ctx.memory
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    step = route_step(w, m, ctx.policy, ctx.knowledge, plan_avoid, plan_costly, map_id, pos, goal)
    if step is not None:
        note = nav_stuck.level_note(nav_stuck.active(m, w))
        return StateOutcome([set_position(step)], f"{reason}{note}", state=InvestigateState.name)
    if nav_stuck.awaiting_break(m, w, goal):
        return StateOutcome(None, f"{reason}; stuck: break", state=InvestigateState.name)
    _count_refusal(ctx, reject_key)
    return StateOutcome(None, f"{reason}; no route", state=InvestigateState.name)


def _count_refusal(ctx: PlayContext, reject_key: str) -> None:
    rejections = ctx.memory.investigate_rejections
    rejections[reject_key] = rejections.get(reject_key, 0) + 1
