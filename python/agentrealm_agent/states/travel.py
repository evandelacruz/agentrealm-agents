"""Travel: walk to plan travel destinations (A27)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import route_first_leg
from ..navigation import stuck as nav_stuck
from ..pathing import goto_navigation_pending, grid_params, guided_step, nav_search, next_step
from ..travel.ops import current_travel_op, set_travel_index
from ..travel.resolve import ResolvedDestination, at_destination, resolve_travel
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome, scripted_outcome
from .intents import set_position


def next_travel_target(world: WorldModel, ctx: PlayContext) -> tuple[int, ResolvedDestination] | None:
    """The first op from the stack's current index that resolves and is not
    already reached, with its index. Read-only: ``act`` moves the stack."""
    m = ctx.memory
    for i in range(m.travel_index, len(m.travel_ops)):
        dest = resolve_travel(m.travel_ops[i], world, ctx.knowledge, m.strength)
        if dest is not None and not at_destination(world, dest):
            return i, dest
    return None


def _arrived(world: WorldModel, ctx: PlayContext) -> bool:
    op = current_travel_op(ctx.memory)
    if op is None:
        return False
    dest = resolve_travel(op, world, ctx.knowledge, ctx.memory.strength)
    return dest is not None and at_destination(world, dest)


class TravelState(State):
    """Priority 5, above Explore. Stack semantics (PLAN.md A27): an op that
    does not resolve yet (``travel:shop`` before any priced supply is seen)
    is skipped; it is dropped once Travel acts on a later op, and kept while
    no later op resolves, so Explore runs until the knowledge base can
    resolve it. Arriving drops the op."""

    name = "Travel"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if goto_navigation_pending(world, ctx.memory, ctx.policy):
            return False
        return _arrived(world, ctx) or next_travel_target(world, ctx) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        target = next_travel_target(world, ctx)
        if target is None:
            # Arrived at the last resolvable op: drop it and give the window to Explore.
            set_travel_index(m, m.travel_index + 1)
            return _fallback(world, ctx, "travel: arrived")
        index, dest = target
        set_travel_index(m, index)
        _, plan_avoid, plan_costly = plan_sets(world, m, policy, ctx.knowledge)
        reflex = reflex_outcome(world, policy, never_attack=ctx.never_attack, state=self.name)
        if reflex is not None:
            return reflex
        out = _travel_step(world, m, policy, dest, ctx.knowledge, plan_avoid, plan_costly)
        if out is not None:
            return out
        return _fallback(world, ctx, f"travel:{dest.label} blocked")


def _fallback(world: WorldModel, ctx: PlayContext, why: str) -> StateOutcome:
    out = scripted_outcome(
        world,
        ctx.memory,
        ctx.policy,
        ctx.rng,
        never_attack=ctx.never_attack,
        knowledge=ctx.knowledge,
        plan=ctx.plan,
        state=TravelState.name,
    )
    out.reason = f"{why}; {out.reason}"
    return out


def _travel_step(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    dest: ResolvedDestination,
    knowledge: KnowledgeBase | None,
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
) -> StateOutcome | None:
    """One step along the route to ``dest``, across maps through known doors (A26).
    None when no step can be planned.

    Stuck detection and escalation drive each map leg, including the first leg
    toward a known door on another map (A15); escalation step 4 is M9.
    """
    goal = f"travel:{dest.label}"
    step = route_step(w, m, policy, knowledge, plan_avoid, plan_costly, dest.map_id, dest.pos, goal)
    if step is None:
        return None
    note = nav_stuck.level_note(nav_stuck.active(m, w))
    label = m.path[-1] if m.path else step
    return StateOutcome([set_position(step)], f"{goal} → {label}{note}", state=TravelState.name)


def route_step(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
    dest_map: int,
    dest: Pos,
    goal: str,
) -> Pos | None:
    """One step toward ``dest_map:dest`` under ``goal``, across maps through
    known door warps (A26), with stuck escalation on this map's leg (A15).

    None when no step can be planned. With no known route to another map the
    goal's path is cleared and nothing backs off. Travel and Investigate's
    cross-map looks (A30) both walk with this.
    """
    plan = _route_plan(m, w, policy, knowledge, plan_avoid, plan_costly, dest_map, dest, goal)
    leg = _map_leg(m, w, goal, dest_map, dest, plan_avoid, plan)
    if leg is None:
        if m.goal == goal:
            m.path, m.goal = [], ""
        return None

    def params():
        return grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m, w=w, knowledge=knowledge)

    return guided_step(m, w, goal, leg, plan_avoid, plan, knowledge, params=params)


def _map_leg(
    m: Memory,
    w: WorldModel,
    goal: str,
    dest_map: int,
    dest: Pos,
    plan_avoid: set[Pos],
    plan,
) -> nav_stuck.Leg | None:
    """What this map's leg toward ``dest`` tracks: ``dest`` itself on its own
    map, else the door the route walks to (A15). A kept path to that door
    needs no new route search."""
    if dest_map == w.map_id:
        return nav_stuck.Leg(dest)
    kept = nav_stuck.leg_toward(m, w, goal, dest_map, dest, None)
    if kept is not None and m.goal == goal and next_step(w, plan_avoid, m.path):
        return kept
    return nav_stuck.leg_toward(m, w, goal, dest_map, dest, plan(None))


def _route_plan(
    m: Memory,
    w: WorldModel,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
    dest_map: int,
    dest: Pos,
    goal: str,
):
    """Plan the current map leg toward ``dest`` (A26), at the attempt's fog price.

    Routes once per fog price this decision, so the leg lookup and
    ``guided_step`` share one door-graph search.
    """
    routes: dict[int, list[Pos] | None] = {}

    def plan(att):
        params = grid_params(
            policy, plan_avoid, plan_costly, allow_goal_door=True, m=m, w=w, knowledge=knowledge
        )
        if params.fog_cost not in routes:
            nav = nav_search(m, w, goal, dest) if dest_map == w.map_id else None
            routes[params.fog_cost] = route_first_leg(w, knowledge, dest_map, dest, params, nav=nav)
        return routes[params.fog_cost]

    return plan
