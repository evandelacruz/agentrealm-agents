"""Retreat: walk to safety before the next hits could kill (A9).

Priority 1. Runs with a hostile in range, somewhere safe to head for
(``retreat_goal``: a known safe tile, or the town cell; it walks to the
nearest one a path reaches outside every known hostile's ground, the refuge
Flee also runs toward, ``pathing.retreat_safe_goal``), and
``should_retreat`` from health and the threat table; never on a safe tile or
during a boss fight (A38).
"""

from __future__ import annotations

import dataclasses

from ..directives import attack_forbidden
from ..hostile_ground import ground_by_hostile
from ..memory import Memory
from ..navigation import cost_path, no_way, oscillation
from ..navigation import stuck as nav_stuck
from ..pathing import flee_step, grid_params, nav_search, next_step, retreat_safe_goal
from ..survival import (
    LOSING_NAV,
    RETREAT_NAV,
    combat_group,
    hostiles_reaching,
    is_attacker,
    on_safe_tile,
    pursuer_peaks,
    retreat_goal,
    safe_goals,
    should_retreat,
    town_cell,
    would_lose,
)
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .boss import boss_fight_on
from .explore import plan_sets
from .fight import engage, in_weapon_reach, weapon_has_hurt
from .heal import use_carried_heal
from .intents import set_position

# Ticks the distance to safety gets to shrink while hits land before
# Retreat calls itself losing ground (A9). As Flee's probe: ticks, not
# decisions, since a step takes several ticks.
RETREAT_PROBE_TICKS = 30


class RetreatState(State):
    """Priority 1. Walks toward ``retreat_goal`` while retreat conditions hold.

    It sends the whole path as one queue, as a walk does, and lets that
    queue run: a reflex replacing it every round trip got one step out of
    each queue (A16 Walk run 4). Its path weighs no danger from the hostiles
    it runs from (``survival.pursuer_peaks``) and goes round the ground of any
    other known hostile, in view or remembered (``hostile_ground.ground_by_hostile``).
    When it is losing ground, it drinks or
    eats what it carries, fights back a hitter its weapon has hurt and the
    win estimate says it beats, or else replans weighing no hostile at all
    (``retreat_step``). With no refuge outside every known hostile's ground,
    it steps away from the hostiles near (``_no_refuge``)."""

    name = "Retreat"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if boss_fight_on(world, ctx.memory):
            return False  # Boss retreats out or commits (A38)
        if on_safe_tile(world):
            return False
        if retreat_goal(world, ctx.knowledge) is None:
            return False
        return should_retreat(world, ctx.policy, ctx.params)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        if boss_fight_on(world, ctx.memory):
            return True
        return on_safe_tile(world) or not should_retreat(world, ctx.policy, ctx.params)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return retreat_step(world, ctx, self.name)


def retreat_step(
    w: WorldModel, ctx: PlayContext, state: str, paced: set[Pos] | None = None, *, step_away: bool = True
) -> StateOutcome:
    """A walk queue along a path to the refuge: the nearest safe cell a path
    reaches outside every known hostile's ground, else the town cell
    (``pathing.retreat_safe_goal``). With no refuge it steps away from the
    hostiles near (``_no_refuge``) when ``step_away``, else sends no intent.

    While the queue it sent is still running, it holds the round (``wait``,
    not a reflex), so the runner lets the queue walk. Losing ground
    (``losing_ground``) is the exception: see ``RetreatState``.

    **Retreat** runs it, and so does **Flee** when running away is not working
    (A9); Flee passes the oscillation escape it already took as ``paced``,
    and ``step_away=False``: with no refuge, its own escape runs instead.

    A goal the walk gets no nearer to in a stuck window (``no_progress``) is
    ruled out for ``SAFE_UNREACHABLE_TICKS`` and the next one is taken; the
    town cell, the last resort, is only planned again while it has a path; a
    stuck window with no step to it rules it out too. A goal proven walled in
    from its own side (``no_way``) is ruled out at once, the town cell too. Either way the
    next decision takes the next one: never the same empty decision for
    good. A path whose first cell is not open rules nothing out.
    """
    m, policy = ctx.memory, ctx.policy
    goals = safe_goals(w, ctx.knowledge)
    if not goals:
        return StateOutcome(None, "nowhere safe known", state=state)
    if w.pos == goals[0]:
        return StateOutcome(None, "at safe tile", state=state)
    # The oscillation guard caught Flee/Retreat pacing: the paced cell is
    # shut, so a path stepping onto it is replanned around it below (A15).
    escape = oscillation.take_escape(m, w) if paced is None else paced
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
    # The nearest safe cell a path reaches, else town (free-play run 2). The
    # escape cells shut only this decision's route, not where it may lead.
    goal = retreat_safe_goal(m, w, policy, ctx.knowledge, plan_avoid, plan_costly)
    if goal is not None and no_progress(w, m, goal):
        # Reachable by budget but no nearer in a whole stuck window: rule it
        # out for a while and take the next candidate, else town (free-play
        # run 3). Town is the last resort: while a path to it exists its walk
        # only plans afresh, but a whole stuck window with no step to it at
        # all rules it out too (free-play run 6: Park stood 60 s planning
        # nothing toward town).
        has_path = m.goal == "safe" and bool(m.path)  # a partial path need not end on the goal
        if goal != town_cell(w, ctx.knowledge) or not has_path:
            m.safe_unreachable[(w.map_id, goal)] = w.tick
        if m.goal == "safe":
            m.path = []
        m.retreat_walk = None
        goal = retreat_safe_goal(m, w, policy, ctx.knowledge, plan_avoid, plan_costly)
    lasting = grid_params(policy, set(plan_avoid), set(plan_costly))  # without this decision's escape: what no_way proves
    plan_avoid |= escape
    if goal is None:
        if step_away:
            return _no_refuge(w, ctx, state, plan_avoid)
        return StateOutcome(None, "no refuge outside hostile ground", state=state)
    losing = losing_ground(w, ctx, goal)
    if losing:
        if out := _turn_on_losing(w, ctx, state):
            m.retreat_paused = (goal, w.tick)  # a drink or a swing is not the walk getting stuck
            return out
        m.path = []  # replan below, weighing no hostile
    elif not escape and m.held_queue is not None and m.state == state and m.retreat_walk == goal:
        return StateOutcome(None, f"retreat → safe {goal}: queue under way", state=state, wait=True)
    pursuers = pursuer_peaks(w, policy, everyone=losing)
    # Ground a hostile it is not running from holds, in view or remembered,
    # costs a detour, and a kept path that now crosses the reach of one it
    # did not cross when planned is planned again (A63 run 4).
    plan_costly |= set().union(*(cells for key, cells in ground_by_hostile(w, policy).items() if key not in pursuers))
    params = dataclasses.replace(grid_params(policy, plan_avoid, plan_costly), danger_peaks=pursuers)
    # Each danger profile keeps its own corridor (``RETREAT_NAV``, ``LOSING_NAV``).
    nav_key = LOSING_NAV if losing else RETREAT_NAV
    new_threat = bool(hostiles_reaching(w, policy, m.path, skip=pursuers) - m.planned_threats)
    nav = nav_search(m, w, nav_key, goal)
    if m.goal != "safe" or not m.path or m.path[-1] != goal or new_threat:
        m.path = cost_path(w, goal, params, nav=nav) or []
        m.goal = "safe"
        m.planned_threats = hostiles_reaching(w, policy, m.path, skip=pursuers)
    step = next_step(w, plan_avoid, m.path)
    if step is None and m.path:
        m.path = cost_path(w, goal, params, nav=nav) or []
        step = next_step(w, plan_avoid, m.path)
    if step is None:
        if m.path:
            # A path whose first cell is not open this decision (someone stands on it, or it is unseen): wait.
            return StateOutcome(None, f"safe {goal}: next step not open", state=state)
        if escape:
            # Shut only for this decision: the next one plans without it (review on #140).
            return StateOutcome(None, f"safe {goal}: no path past the paced cell", state=state)
        if not no_way(w, goal, lasting):
            # No step found is not no path: a budgeted search can miss a way
            # round, and one from our side that runs out shows only that where
            # we stand is shut, maybe for this decision alone. ``no_progress``
            # rules the cell out, the town cell too, if it goes on for a stuck
            # window.
            return StateOutcome(None, f"safe {goal}: no step found", state=state)
        # Proven from the goal's side: walled in. Rule the cell out, the town
        # cell too, so the next decision takes the next safe cell instead of
        # planning the same nothing again (free-play runs 5 and 6: Park stood
        # 60 s on one cell).
        m.safe_unreachable[(w.map_id, goal)] = w.tick
        return StateOutcome(None, f"safe {goal}: no path, ruled out", state=state)
    # The runner queues the walkable prefix of ``m.path`` from this first step,
    # and leaves these out of its threats: the path weighs none of them. Set
    # only here, so a walk that falls through to another state never inherits it.
    m.retreat_walk, m.walk_skip = goal, set(pursuers)
    reason = f"retreat → safe {goal}" + (" (losing ground)" if losing else "")
    return StateOutcome([set_position(step)], reason, reflex=True, state=state)


def _no_refuge(w: WorldModel, ctx: PlayContext, state: str, blocked: set[Pos]) -> StateOutcome:
    """No refuge: every safe cell lies in a known hostile's ground or has no
    way to it (``pathing.retreat_safe_goal``). Open distance from the
    hostiles in range and the one hitting us, the best single step away
    (``pathing.flee_step``), as Flee would; nothing when none is near (Park
    with nothing threatening) or no step opens distance (cornered).

    A safe cell a pursuer gets to first is not taken instead: walking back
    toward it is the pacing free-play run 7 died of (6 hits in 19 s)."""
    m = ctx.memory
    hostiles = combat_group(w, ctx.policy) + [e for e in w.entities if is_attacker(w, e)]
    step = flee_step(w, hostiles, blocked) if hostiles else None
    if step is None:
        return StateOutcome(None, "no refuge outside hostile ground", state=state)
    m.path, m.retreat_walk = [], None  # a step away, not a walk to safety
    return StateOutcome([set_position(step)], "no refuge outside hostile ground: open distance", reflex=True, state=state)


def no_progress(w: WorldModel, m: Memory, goal: Pos) -> bool:
    """The walk to ``goal`` has gone a stuck window (``PROGRESS_TICK_LIMIT``)
    without its remaining path shortening (A9, A66).

    A15's progress record, without its escalation ladder: the attempt for
    the ``safe`` walk at ``goal`` is sampled each call (``nav_stuck.observe``)
    but never made active, so it changes no other walk's window or the
    oscillation guard. A decision that skips it starts the window over
    (``nav_stuck.resume``). The window pauses while a losing Retreat drinks
    or fights back instead of walking (``Memory.retreat_paused``): the time
    since that decision is not counted. Once it reports, the attempt is
    dropped, so the goal gets a fresh window if it is picked again.
    """
    att = nav_stuck.attempt(m, w, "safe", goal)
    if att is None:
        return False
    nav_stuck.resume(m, att, w.tick)
    paused, m.retreat_paused = m.retreat_paused, None
    if paused is not None and paused[0] == goal:
        att.window_tick = min(w.tick, att.window_tick + w.tick - paused[1])
    started = att.window_tick if att.best is None else None
    nav_stuck.observe(att, w, m.path if m.goal == "safe" and m.path and m.path[-1] == goal else None)
    if started is not None:
        att.window_tick = started  # the first path planned sets the bar; it is not progress
    if w.tick - att.window_tick < nav_stuck.PROGRESS_TICK_LIMIT:
        return False
    nav_stuck.finish(m, att)
    return True


def losing_ground(w: WorldModel, ctx: PlayContext, goal: Pos) -> bool:
    """Over the last ``RETREAT_PROBE_TICKS``, the distance to ``goal`` has not
    shrunk and a hostile hit landed (A9).

    Samples the distance at each call; a new goal, or a gap longer than the
    window since the last sample, starts the record over. Once it reports
    losing, the window starts over, so the fallback runs once per window.
    """
    m = ctx.memory
    gap = chebyshev(w.pos, goal)
    since = w.tick - RETREAT_PROBE_TICKS
    if m.retreat_to != goal or not m.retreat_gaps or m.retreat_gaps[-1][0] < since:
        m.retreat_to, m.retreat_gaps = goal, []
    m.retreat_gaps.append((w.tick, gap))
    # Keep one sample at or before the window's start, nothing older.
    while len(m.retreat_gaps) > 1 and m.retreat_gaps[1][0] <= since:
        del m.retreat_gaps[0]
    start_tick, start_gap = m.retreat_gaps[0]
    hit = w.attacked_tick is not None and w.attacked_tick > since and w.damage_since(since + 1) > 0
    if start_tick > since or gap < start_gap or not hit:
        return False
    m.retreat_gaps = [(w.tick, gap)]
    return True


def _turn_on_losing(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome | None:
    """Losing ground: drink or eat what we carry, else fight back a hitter we beat, else None."""
    out = use_carried_heal(w, ctx.memory)
    if out is None:
        hitter = _beatable_hitter(w, ctx)
        out = engage(w, ctx, hitter, state) if hitter is not None else None
    if out is None or not out.intents:
        return None
    out.state, out.reflex = state, True
    out.reason = f"retreat losing ground: {out.reason}"
    return out


def _beatable_hitter(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The hostile hitting us, when it is in weapon reach, ours to swing at, a
    type our weapon has hurt, and the win estimate says we beat it."""
    for e in w.entities:
        if not is_attacker(w, e) or attack_forbidden(e, ctx.never_attack):
            continue
        if not in_weapon_reach(w, e, ctx.knowledge) or not weapon_has_hurt(w, e, ctx.knowledge):
            continue
        if would_lose(w, ctx.policy, ctx.params):
            return None
        return e
    return None
