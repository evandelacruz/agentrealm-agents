"""Travel: carry out the plan's ``travel`` op (A27).

Resolves the destination (``travel/resolve.py``: a point, the town, the
nearest known shop or hunting ground, an entrance), then walks there across
maps through known door warps (A26), with stuck escalation on each map's leg
(A15). ``entrance`` at ``0, 0`` walks to the nearest unexplored door.
Arriving finishes the op.

A ``hunting_ground`` with none known searches for one: it explores the
map's frontier while spare windows read zones in widening rings around the
character (``zone_discovery.hunt_probe``), until a read finds a hunting cell
it may enter, the frontier runs out, or ``HUNT_SEARCH_SECONDS`` pass.
"""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import HuntSearch, Memory
from ..navigation import doors_goal_path, route_first_leg
from ..navigation import stuck as nav_stuck
from ..navigation import walk as nav_walk
from ..pathing import grid_params, guided_step, nav_search, next_step
from ..plan import EXPLORE_ANYWHERE, GoalOp, explore_targets
from ..travel.ops import travel_op_from_plan_goal
from ..travel.resolve import ResolvedDestination, at_destination, resolve_travel
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .explore import explore_outcome, plan_sets
from .intents import set_position

# A hunting-ground search gives up after this long (A27).
HUNT_SEARCH_SECONDS = 300
# A search Travel has not worked on for this long starts over: the op was
# off the top (a Retreat, a newer op) and is back.
HUNT_SEARCH_RESUME_SECONDS = 60
# Spare windows read zones for the search while Travel worked on it this
# recently. Decisions came 5–20 s apart in A23 survive-a-fight run 2, so a
# shorter window left the probes off most of the search.
HUNT_PROBE_FRESH_SECONDS = 30
# What a hunting-ground search explores: the whole map's frontier.
HUNT_SEARCH_AREA: GoalOp = {"op": "explore_area", "x": 0, "y": 0, "radius": EXPLORE_ANYWHERE}


class TravelState(State):
    """Executor for ``travel``. With no destination the knowledge base can
    resolve yet (``travel:shop`` before any priced supply is seen) it sends
    nothing: the op stalls and is dropped (A34). A ``hunting_ground`` with
    none known searches for one instead (``search_hunting_ground``)."""

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
        if dest is None and op["to"] == "hunting_ground":
            return search_hunting_ground(world, ctx, op)
        m.hunt_search = None
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
        first = m.path[0] if m.goal == goal and m.path else None
        if first is not None and not nav_stuck.awaiting_break(m, world, goal):
            # The route's first step is taken by an occupant, or still unseen on
            # a path the walk is under way on (A15, A58 run 5): hold while its
            # window runs, rather than let the safe default step away and back.
            # A wait is not progress: the op's stall clock runs, so a permanent
            # occupant cannot pin the stack, and the fog hold ends after
            # ``walk.FOG_HOLD_TICKS``.
            if first in world.occupied():
                return StateOutcome(None, f"{goal}: way taken, waiting", state=self.name, wait=True, progress=False)
            if first not in world.view.tiles and nav_walk.hold_for_fog(m.walks.get(goal), world):
                return StateOutcome(None, f"{goal}: next cell unseen, waiting", state=self.name, wait=True, progress=False)
        # Stuck at step 2: Break, below, opens the way this decision.
        return StateOutcome(None, f"{goal} blocked", state=self.name)


def search_hunting_ground(world: WorldModel, ctx: PlayContext, op: GoalOp) -> StateOutcome:
    """No hunting ground known: explore the frontier while spare windows read
    zones for one (A27). Bounded: the op is dropped once the frontier runs
    out or the search has run ``HUNT_SEARCH_SECONDS``."""
    m, plan = ctx.memory, ctx.plan
    assert plan is not None
    s = m.hunt_search
    if s is None or s.op != op or world.tick - s.last > HUNT_SEARCH_RESUME_SECONDS * plan.tick_hz:
        s = m.hunt_search = HuntSearch(dict(op), world.tick, world.tick, world.tick)
    s.last = world.tick
    s.probe_until = world.tick + HUNT_PROBE_FRESH_SECONDS * plan.tick_hz
    gave_up = ""
    if world.tick - s.since >= HUNT_SEARCH_SECONDS * plan.tick_hz:
        gave_up = f"no hunting ground found in {HUNT_SEARCH_SECONDS}s of searching"
    elif not explore_targets(HUNT_SEARCH_AREA, world):
        gave_up = "no hunting ground found and nothing left to explore"
    if gave_up:
        m.hunt_search = None
        plan.drop_current(gave_up, memory=m)
        return StateOutcome(None, f"travel:hunting_ground: {gave_up}", state=TravelState.name)
    out = explore_outcome(
        world, m, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=HUNT_SEARCH_AREA, state=TravelState.name
    )
    out.reason = f"travel:hunting_ground searching: {out.reason}"
    return out


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
    return resolve_travel(t, w, ctx.knowledge, ctx.memory.strength, ctx.memory.nav_stuck.given_up_travel)


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
    goal's path is cleared and nothing backs off.
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
