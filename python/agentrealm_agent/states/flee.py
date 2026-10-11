"""Flee: open distance from hostiles, toward the refuge Retreat walks to (A9)."""

from __future__ import annotations

import dataclasses

from ..directives import attack_forbidden
from ..memory import Memory
from ..navigation import cost_path, oscillation
from ..pathing import flee_run, flee_step, grid_params, outruns, retreat_safe_goal, step_open
from ..engagement import cannot_outrun, fight_or_flee, fights
from ..survival import flee_from, on_safe_tile
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .boss import boss_fight_on
from .explore import plan_sets
from .fight import can_engage, engage, fight_target
from .intents import set_position
from .retreat import retreat_step

# Ticks the gap to the nearest hostile gets to grow before Flee calls the
# escape failed (A9). Ticks, not decisions: under urgent polling Flee decides
# every tick or two, and a step takes several ticks.
FLEE_PROBE_TICKS = 30


def _committed_step(w: WorldModel, m: Memory, hostiles: list[Entity], blocked: set[Pos]) -> Pos | None:
    """The next cell of ``m.flee_path``, or None when the escape has arrived or is blocked.

    Drops the cells already walked. Blocked means the next cell is shut or
    out of reach, or the hostiles have moved so that it now lands nearer the
    nearest of them than ``flee_step``'s best step would.
    """
    path = m.flee_path
    if w.pos in path:
        del path[: path.index(w.pos) + 1]
    if not path or not step_open(w, blocked, path[0]):
        return None
    nxt, best = path[0], flee_step(w, hostiles, blocked) or w.pos
    if min(chebyshev(nxt, h.pos) for h in hostiles) < min(chebyshev(best, h.pos) for h in hostiles):
        return None
    return nxt


def flee_escape(
    w: WorldModel, m: Memory, ctx: PlayContext, hostiles: list[Entity], blocked: set[Pos]
) -> list[Pos]:
    """A multi-step escape, or [] when standing still is best (A9, A58).

    It runs toward Retreat's refuge (``pathing.retreat_safe_goal``: the
    nearest safe cell outside every known hostile's ground), so Flee and
    Retreat never pull opposite ways (free-play run 7). The first step is
    ``flee_step``'s, so ties break toward the refuge. The rest is a route
    from there to the refuge when the agent reaches every cell of it before
    any hostile could (``outruns``), else ``flee_run`` away from the
    hostiles. A refuge behind a hostile is Retreat's to reach, not Flee's.
    """
    policy = ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
    # On purpose, the same call Retreat makes, side effects included: it
    # commits the refuge (``RETREAT_TARGET``) that Retreat then keeps, and
    # its path checks are the ones Retreat would run. Only when a new escape
    # is planned, not every decision.
    refuge = retreat_safe_goal(m, w, policy, ctx.knowledge, plan_avoid, plan_costly)
    first = flee_step(w, hostiles, blocked, [refuge] if refuge is not None else ())
    if first is None:
        return []
    if refuge is not None:
        if refuge == first:
            return [first]
        params = grid_params(policy, blocked, plan_costly)
        rest = cost_path(dataclasses.replace(w, pos=first), refuge, params)
        if rest and outruns([first] + rest, hostiles):
            return [first] + rest
    return flee_run(w, hostiles, blocked, first)


def hit_while_fleeing(w: WorldModel, m: Memory) -> bool:
    """A hostile hit us (``WorldModel.attacked_tick``) since Flee began."""
    return w.attacked_tick is not None and w.attacked_tick > m.flee_since


def not_outrunning(w: WorldModel, m: Memory) -> bool:
    """Fleeing is a death march: a hostile hit us since Flee began, or the gap
    to the nearest hostile has not grown over the last ``FLEE_PROBE_TICKS`` (A9)."""
    tick, gap = m.flee_gaps[-1]
    earlier = [g for t, g in m.flee_gaps if t <= tick - FLEE_PROBE_TICKS]
    return bool(earlier and gap <= earlier[-1]) or hit_while_fleeing(w, m)


def instead_of_fleeing(
    w: WorldModel, ctx: PlayContext, target: Entity, paced: set[Pos], fight: bool
) -> StateOutcome | None:
    """Once running away cannot open distance: carry out the engagement's
    decision (``fight``, ``engagement.cannot_outrun``), or None to keep running.

    Fight: swing at or close on ``target`` unless ``never_attack`` forbids
    it. Flee: walk toward safety, Retreat's way. ``paced`` is the
    oscillation guard's escape, already taken this decision (A15).
    """
    if fight and not attack_forbidden(target, ctx.never_attack):
        out = engage(w, ctx, target, FleeState.name)
    else:
        out = retreat_step(w, ctx, FleeState.name, paced)
    if out.intents or out.wait:
        out.reason = f"not outrunning {target.kind} {target.id}: {out.reason}"
        return out
    return None


def should_flee(world: WorldModel, ctx: PlayContext) -> bool:
    """``policy.on_hostile`` as the README documents it.

    ``flee`` flees every hostile in range, and a pursuer that hit us
    recently even out of range (``flee_from``); ``fight`` flees when there is no
    swingable target (``never_attack``), the engagement's decision is not to
    fight (``engagement.fights``), or the target is out of weapon reach with
    no open step closer; ``ignore``
    never flees. On a known safe tile, nothing can hurt us, so it stays.
    """
    policy = ctx.policy
    if policy.kind != "scripted" or not world.alive or world.pos is None:
        return False
    if boss_fight_on(world, ctx.memory):
        return False  # Boss retreats out or commits (A38)
    if policy.on_hostile == "ignore" or not flee_from(world, policy):
        return False
    if on_safe_tile(world):
        return False
    if policy.on_hostile == "flee":
        return True
    target = fight_target(world, policy, ctx.never_attack)
    if target is None or not fights(world, ctx.memory, policy, ctx.params):
        return True
    return not can_engage(world, target, ctx)


class FleeState(State):
    """Priority 2, after **Fight**. Opens distance per ``policy.on_hostile`` when
    hostiles are in range and we are not on a safe tile; stands down during a
    boss fight (A38).

    Flee commits to a multi-step escape (``flee_escape``) and walks it until
    it arrives, is blocked, or Flee stops running, rather than re-picking the
    greedy best step every decision: against two moving hostiles that
    re-pick sends it back and forth between two cells (A58). Cornered, it
    stands still.

    Running must work: once a hostile hits us after Flee began, or the gap
    to it has not grown over ``FLEE_PROBE_TICKS`` ticks, the engagement is decided again on that
    (``engagement.cannot_outrun``), and Flee carries the decision out
    (``instead_of_fleeing``): it fights back, or walks Retreat's way (A9)."""

    name = "Flee"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return should_flee(world, ctx)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not should_flee(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        hostiles = flee_from(w, policy)
        if not hostiles or w.pos is None:
            return StateOutcome(None, "no hostiles", state=self.name, wait=True)
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, w.pos), e.id))
        blocked, _, _ = plan_sets(w, m, policy, ctx.knowledge)
        if m.state != self.name:
            m.flee_gaps, m.flee_since = [], w.tick
        m.flee_gaps.append((w.tick, chebyshev(target.pos, w.pos)))
        # Keep one sample at or before the probe window's start, nothing older.
        while len(m.flee_gaps) > 1 and m.flee_gaps[1][0] <= w.tick - FLEE_PROBE_TICKS:
            del m.flee_gaps[0]
        # Read once per decision: the oscillation guard caught Flee/Retreat
        # pacing (A15), and whichever escape runs below must keep off these cells.
        paced = oscillation.take_escape(m, w)
        e = fight_or_flee(w, m, policy, ctx.params)
        if e is not None and not e.cannot_outrun and not_outrunning(w, m):
            e = cannot_outrun(w, m, policy, ctx.params)
        if e is not None and e.cannot_outrun:
            instead = instead_of_fleeing(w, ctx, target, paced, e.fight)
            if instead is not None:
                return instead
        # Start over when Flee did not run last decision (the threat was
        # gone in between) or the guard caught pacing; a caught escape keeps
        # off the paced cells.
        if m.state != self.name or paced:
            m.flee_path = []
        away = _committed_step(w, m, hostiles, blocked | m.flee_avoid)
        if away is None:
            m.flee_avoid = paced
            m.flee_path = flee_escape(w, m, ctx, hostiles, blocked | paced)
            away = m.flee_path[0] if m.flee_path else None
        if away is None:
            return StateOutcome(None, "nowhere to flee", state=self.name, wait=True)
        m.path, m.retreat_walk = [], None  # the queue sent now is Flee's own, not a retreat walk
        return StateOutcome([set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=self.name)
