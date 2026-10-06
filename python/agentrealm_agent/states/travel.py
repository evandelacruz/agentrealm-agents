"""Travel: carry out the plan's ``travel`` op (A27).

Resolves the destination (``travel/resolve.py``: a point, the town, the
nearest known shop or hunting ground, an entrance), then walks there across
maps through known door warps (A26), with stuck escalation on each map's leg
(A15). ``entrance`` at ``0, 0`` walks to the nearest unexplored door.
Arriving finishes the op.
"""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import doors_goal_path, route_first_leg
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, guided_step, nav_search, next_step
from ..plan import GoalOp
from ..travel.ops import travel_op_from_plan_goal
from ..travel.resolve import ResolvedDestination, at_destination, resolve_travel
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import set_position


class TravelState(State):
    """Executor for ``travel``. With no destination the knowledge base can
    resolve yet (``travel:shop`` before any priced supply is seen) it sends
    nothing: the op stalls and is dropped (A34)."""

    name = "Travel"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        op = my_op(ctx, self.name)
        assert op is not None and ctx.plan is not None
        dest = resolve_destination(world, ctx, op)
        if dest is None:
            return StateOutcome(None, f"travel:{op['to']} not resolved yet", state=self.name)
        if at_destination(world, dest):
            ctx.plan.finish_current(f"at {dest.label}", memory=m)
            if m.goal == f"travel:{dest.label}":
                m.path, m.goal = [], ""
            return StateOutcome(None, f"travel:{dest.label} arrived", state=self.name)
        if m.goal_op != op:
            # Another travel op's path carries the same label: never walk it for this one.
            if m.goal.startswith("travel:"):
                m.path, m.goal = [], ""
            m.goal_op = dict(op)
        _, plan_avoid, plan_costly = plan_sets(world, m, policy, ctx.knowledge)
        out = _travel_step(world, m, policy, dest, ctx.knowledge, plan_avoid, plan_costly)
        if out is not None:
            return out
        goal = f"travel:{dest.label}"
        if m.goal == goal and m.path and m.path[0] in world.occupied() and not nav_stuck.awaiting_break(m, world, goal):
            # The route's first step is taken by an occupant: hold while its
            # window runs (A15), rather than let the safe default step away and
            # back. A wait is not progress: the op's stall clock runs, so a
            # permanent occupant cannot pin the stack.
            return StateOutcome(None, f"{goal}: way taken, waiting", state=self.name, wait=True, progress=False)
        # Stuck at step 2: Break, below, opens the way this decision.
        return StateOutcome(None, f"{goal} blocked", state=self.name)


def resolve_destination(w: WorldModel, ctx: PlayContext, op: GoalOp) -> ResolvedDestination | None:
    """Where the ``travel`` op goes, or None when the knowledge base cannot say yet."""
    t = travel_op_from_plan_goal(op)
    if t.to == "entrance" and t.x is None:
        _, blocked, costly = plan_sets(w, ctx.memory, ctx.policy, ctx.knowledge)
        params = grid_params(ctx.policy, blocked, costly, allow_goal_door=True, m=ctx.memory, w=w, knowledge=ctx.knowledge)
        path = doors_goal_path(w, ctx.knowledge, params)
        if not path or w.map_id is None:
            return None
        return ResolvedDestination(w.map_id, path[-1], "entrance")
    return resolve_travel(t, w, ctx.knowledge, ctx.memory.strength)


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
    return guided_step(m, w, goal, leg, plan_avoid, plan, knowledge)


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
