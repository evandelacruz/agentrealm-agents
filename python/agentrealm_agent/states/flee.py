"""Flee: open distance from hostiles, toward known safety (A9)."""

from __future__ import annotations

import dataclasses

from ..directives import attack_forbidden
from ..memory import Memory
from ..navigation import cost_path, oscillation
from ..pathing import flee_run, flee_step, grid_params, outruns, step_open
from ..survival import flee_from, hostiles_in_range, is_attacker, on_safe_tile, would_lose
from ..world import Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .boss import boss_fight_on
from .explore import plan_sets
from .fight import can_engage, engage, fight_target, in_weapon_reach
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
    w: WorldModel, m: Memory, ctx: PlayContext, hostiles: list[Entity], blocked: set[Pos], safes: set[Pos]
) -> list[Pos]:
    """A multi-step escape, or [] when standing still is best (A9, A58).

    The first step is ``flee_step``'s, so ties still break toward the nearest
    known safe tile. The rest is a route from there to that safe tile when
    one is known and the agent reaches every cell of it before any hostile
    could (``outruns``), else ``flee_run`` away from the hostiles. A safe
    tile behind a hostile is Retreat's to reach, not Flee's.
    """
    first = flee_step(w, hostiles, blocked, safes)
    if first is None:
        return []
    if safes:
        goal = min(safes, key=lambda s: (chebyshev(s, first), s))
        if goal == first:
            return [first]
        _, _, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
        params = grid_params(ctx.policy, blocked, plan_costly)
        rest = cost_path(dataclasses.replace(w, pos=first), goal, params)
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
    w: WorldModel, ctx: PlayContext, target: Entity, hostiles: list[Entity], blocked: set[Pos], paced: set[Pos]
) -> StateOutcome | None:
    """Fight back or retreat once running away has failed (A9), or None to keep running.

    In order: fight back when the win estimate says we win or there is no
    step away (cornered); walk to the nearest known safe tile, Retreat's
    way; swing back anyway when ``target`` is the one hitting us and in
    weapon reach, since running and retreating both failed. A target
    ``never_attack`` forbids is never fought. ``paced`` is the oscillation
    guard's escape, already taken this decision (A15).
    """
    may_hit = not attack_forbidden(target, ctx.never_attack)
    cornered = flee_step(w, hostiles, blocked) is None
    wins = bool(hostiles_in_range(w, ctx.policy)) and not would_lose(w, ctx.policy, ctx.params)
    options = []
    if may_hit and (cornered or wins):
        options.append(lambda: engage(w, ctx, target, FleeState.name))
    options.append(lambda: retreat_step(w, ctx, FleeState.name, paced))
    hitter_in_reach = is_attacker(w, target) and in_weapon_reach(w, target, ctx.knowledge)
    if may_hit and hit_while_fleeing(w, ctx.memory) and hitter_in_reach:
        options.append(lambda: engage(w, ctx, target, FleeState.name))
    for option in options:
        out = option()
        if out.intents:
            out.reason = f"not outrunning {target.kind} {target.id}: {out.reason}"
            return out
    return None


def should_flee(world: WorldModel, ctx: PlayContext) -> bool:
    """``policy.on_hostile`` as the README documents it.

    ``flee`` flees every hostile in range, and a pursuer that hit us
    recently even out of range (``flee_from``); ``fight`` flees when there is no
    swingable target (``never_attack``), the win estimate says we lose, or
    the target is out of weapon reach with no open step closer; ``ignore``
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
    if target is None or would_lose(world, policy, ctx.params):
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
    to it has not grown over ``FLEE_PROBE_TICKS`` ticks, Flee fights back
    or retreats instead (``instead_of_fleeing``) until it stops (A9)."""

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
            m.flee_gaps, m.flee_since, m.flee_failed = [], w.tick, False
        m.flee_gaps.append((w.tick, chebyshev(target.pos, w.pos)))
        # Read once per decision: the oscillation guard caught Flee/Retreat
        # pacing (A15), and whichever escape runs below must keep off these cells.
        paced = oscillation.take_escape(m, w)
        if m.flee_failed or not_outrunning(w, m):
            m.flee_failed = True
            instead = instead_of_fleeing(w, ctx, target, hostiles, blocked, paced)
            if instead is not None:
                return instead
        safes = safe_tiles(w, w.map_id) if w.map_id is not None else set()
        # Start over when Flee did not run last decision (the threat was
        # gone in between) or the guard caught pacing; a caught escape keeps
        # off the paced cells.
        if m.state != self.name or paced:
            m.flee_path = []
        away = _committed_step(w, m, hostiles, blocked | m.flee_avoid)
        if away is None:
            m.flee_avoid = paced
            m.flee_path = flee_escape(w, m, ctx, hostiles, blocked | paced, safes)
            away = m.flee_path[0] if m.flee_path else None
        if away is None:
            return StateOutcome(None, "nowhere to flee", state=self.name, wait=True)
        m.path = []
        return StateOutcome([set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=self.name)
