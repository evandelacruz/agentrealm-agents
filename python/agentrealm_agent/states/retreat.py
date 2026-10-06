"""Retreat: walk to safety before the next hits could kill (A9).

Priority 1. Runs with a hostile in range, somewhere safe to head for
(``retreat_goal``: a known safe tile, or the town cell), and
``should_retreat`` from health and the threat table; never on a safe tile or
during a boss fight (A38).
"""

from __future__ import annotations

import dataclasses

from ..directives import attack_forbidden
from ..navigation import cost_path, oscillation
from ..pathing import grid_params, nav_search, next_step
from ..survival import combat_group, is_attacker, on_safe_tile, retreat_goal, should_retreat, would_lose
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
    it runs from (``_pursuers``). When it is losing ground, it drinks or
    eats what it carries, fights back a hitter its weapon has hurt and the
    win estimate says it beats, or else replans weighing no hostile at all
    (``retreat_step``)."""

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


def retreat_step(w: WorldModel, ctx: PlayContext, state: str, paced: set[Pos] | None = None) -> StateOutcome:
    """A walk queue along a path to ``retreat_goal``, or no intent when there is none.

    While the queue it sent is still running, it holds the round (``wait``,
    not a reflex), so the runner lets the queue walk. Losing ground
    (``losing_ground``) is the exception: see ``RetreatState``.

    **Retreat** runs it, and so does **Flee** when running away is not working
    (A9); Flee passes the oscillation escape it already took as ``paced``.
    """
    m, policy = ctx.memory, ctx.policy
    goal = retreat_goal(w, ctx.knowledge)
    if goal is None:
        return StateOutcome(None, "nowhere safe known", state=state)
    if w.pos == goal:
        return StateOutcome(None, "at safe tile", state=state)
    # The oscillation guard caught Flee/Retreat pacing: the paced cell is
    # shut, so a path stepping onto it is replanned around it below (A15).
    escape = oscillation.take_escape(m, w) if paced is None else paced
    losing = losing_ground(w, ctx, goal)
    if losing:
        if out := _turn_on_losing(w, ctx, state):
            return out
        m.path = []  # replan below, weighing no hostile
    elif not escape and m.held_queue is not None and m.state == state and m.retreat_walk == goal:
        return StateOutcome(None, f"retreat → safe {goal}: queue under way", state=state, wait=True)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
    plan_avoid |= escape
    params = dataclasses.replace(grid_params(policy, plan_avoid, plan_costly), danger_peaks=_pursuers(w, ctx, losing))
    if m.goal != "safe" or not m.path or m.path[-1] != goal:
        m.path = cost_path(w, goal, params, nav=nav_search(m, w, "safe", goal)) or []
        m.goal = "safe"
    step = next_step(w, plan_avoid, m.path)
    if step is None and m.path:
        m.path = cost_path(w, goal, params, nav=nav_search(m, w, "safe", goal)) or []
        step = next_step(w, plan_avoid, m.path)
    if step is None:
        return StateOutcome(None, "safe tile unreachable", state=state)
    # The runner queues the walkable prefix of ``m.path`` from this first step.
    m.retreat_walk = goal
    reason = f"retreat → safe {goal}" + (" (losing ground)" if losing else "")
    return StateOutcome([set_position(step)], reason, reflex=True, state=state)


def _pursuers(w: WorldModel, ctx: PlayContext, everyone: bool) -> dict[tuple[str, int], int]:
    """Danger peak 0 for the hostiles we are running from: the fight's group and
    whoever hit us last, or every hostile in view when ``everyone``.

    So the path takes the shortest way to safety instead of detouring round
    a chaser that follows anyway (A23 survive-a-fight run 1), and still
    keeps clear of hostiles it has not met. Their cells stay occupied, so it
    never runs through one.
    """
    if everyone:
        chasing = [e for e in w.entities if e.kind in ctx.policy.hostile]
    else:
        chasing = combat_group(w, ctx.policy) + [e for e in w.entities if is_attacker(w, e)]
    return {(e.kind, e.id): 0 for e in chasing}


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
