"""Fight: win-estimate gated combat with retreat queued behind attacks (A23).

Also the executor for the plan's ``fight`` op (A114): the planner names an
NPC to fight, and Fight closes on it and swings. No attack is made from a
safe zone, so a swing is only ever sent from a cell outside one: on a safe
tile, Fight's move is the step off it toward the target.
"""

from __future__ import annotations

import dataclasses

from ..directives import attack_forbidden
from ..executor import build_attack_queue, queue_horizon_intents
from ..executor.intents import step
from ..executor.movement import direction_between
from ..knowledge_base import KnowledgeBase
from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step, retreat_safe_goal
from ..reach import weapon_reach
from ..engagement import Engagement, fights, held_members
from ..survival import RETREAT_NAV, at_health_floor, combat_group, is_hostile, on_safe_tile, pursuer_peaks
from ..plan import op_npc
from ..world import Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import known_safe
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import set_position, use_npc, use_on


# ``Use`` swings queued per submit, ahead of the retreat tail.
ATTACK_USES = 3


def fight_target(w: WorldModel, policy, never_attack: list[str], engagement: Engagement | None = None) -> Entity | None:
    """Nearest hostile in range (``survival.is_hostile``) that ``on_hostile = fight`` may swing at.

    With none in range, the nearest member still in view of ``engagement``
    when its decision is fight: a fight goes on until it ends (A113,
    ``engagement.held_members``), not only while the hostile stays in range.
    """
    here = w.pos
    if here is None or policy.on_hostile != "fight":
        return None
    hostiles = [
        e
        for e in w.entities
        if is_hostile(w, policy, e)
        and not e.is_boss  # Boss fights bosses (A38)
        and chebyshev(e.pos, here) <= policy.hostile_range
    ]
    if not hostiles:
        hostiles = [e for e in held_members(w, engagement) if is_hostile(w, policy, e) and not e.is_boss]
    if not hostiles:
        return None
    target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
    if attack_forbidden(target, never_attack):
        return None
    return target


def attack_intent(target: Entity) -> dict:
    if target.kind == "npc":
        return use_npc(target)
    return use_on(target)


def in_weapon_reach(w: WorldModel, target: Entity, knowledge: KnowledgeBase | None) -> bool:
    if w.pos is None:
        return False
    return chebyshev(w.pos, target.pos) <= weapon_reach(w, knowledge)


def _ticks_since_use(w: WorldModel, last_use_tick: int | None) -> int | None:
    if last_use_tick is None:
        return None
    return max(1, w.tick - last_use_tick)


def retreat_tail(
    w: WorldModel,
    m,
    policy,
    ctx: PlayContext,
    *,
    limit: int,
) -> list[dict]:
    """``Step`` intents toward Retreat's goal, the nearest safe cell a path
    reaches (``pathing.retreat_safe_goal``), up to ``limit``."""
    if w.pos is None:
        return []
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
    goal = retreat_safe_goal(m, w, policy, ctx.knowledge, plan_avoid, plan_costly)
    if goal is None or w.pos == goal:
        return []
    # Priced as Retreat prices its walk, so the two share one path and corridor.
    params = dataclasses.replace(grid_params(policy, plan_avoid, plan_costly), danger_peaks=pursuer_peaks(w, policy))
    if m.goal != "safe" or not m.path or m.path[-1] != goal:
        m.path = cost_path(w, goal, params, nav=nav_search(m, w, RETREAT_NAV, goal)) or []
        m.goal = "safe"
    out: list[dict] = []
    pos = w.pos
    path = list(m.path)
    for _ in range(limit):
        nxt = next_step(w, plan_avoid, path)
        if nxt is None:
            break
        out.append(step(direction_between(pos, nxt)))
        pos = nxt
        if path and path[0] == nxt:
            path.pop(0)
        elif nxt in path:
            path = path[path.index(nxt) + 1 :]
        if pos == goal:
            break
    return out


def off_safe_step(w: WorldModel, target: Entity, blocked: set[Pos], reach: int) -> Pos | None:
    """From a safe tile, one step onto a cell outside the safe zone toward
    ``target``: one within ``reach`` of it first, else the nearest to it.
    None when no open step leaves the safe zone without moving away."""
    here = w.pos
    if here is None or w.map_id is None:
        return None
    dist = chebyshev(here, target.pos)
    options = [
        p for p in w.open_neighbours(here, blocked) if not known_safe(w, w.map_id, p) and chebyshev(p, target.pos) <= dist
    ]
    if not options:
        return None
    return min(options, key=lambda p: (chebyshev(p, target.pos) > reach, chebyshev(p, target.pos), p))


def close_step(w: WorldModel, target: Entity, blocked: set[Pos]) -> Pos | None:
    """One step closer to ``target`` when out of weapon reach."""
    here = w.pos
    if here is None:
        return None
    dist = chebyshev(here, target.pos)
    options = [p for p in w.open_neighbours(here, blocked) if chebyshev(p, target.pos) < dist]
    if not options:
        return None
    return min(options, key=lambda p: (chebyshev(p, target.pos), p))


def held_target(world: WorldModel, ctx: PlayContext, engagement: Engagement) -> Entity | None:
    """The hostile Fight goes on with in ``engagement``, a fight decided on:
    the one ``fight_target`` picks, when Fight can swing at it or step closer.
    None when the fight has nothing left Fight can act on (A113); the
    engagement ends on it (``engagement.sync_engagement``)."""
    target = fight_target(world, ctx.policy, ctx.never_attack, engagement)
    if target is None or not can_engage(world, target, ctx):
        return None
    return target


def can_engage(world: WorldModel, target: Entity, ctx: PlayContext) -> bool:
    """In weapon reach off safe ground, or one open step brings us closer
    (or, on a safe tile, off it toward the target)."""
    blocked, _, _ = plan_sets(world, ctx.memory, ctx.policy, ctx.knowledge)
    if on_safe_tile(world):
        return off_safe_step(world, target, blocked, weapon_reach(world, ctx.knowledge)) is not None
    if in_weapon_reach(world, target, ctx.knowledge):
        return True
    return close_step(world, target, blocked) is not None


def held_on_safe_ground(world: WorldModel, ctx: PlayContext, target: Entity) -> bool:
    """On a safe tile at the health floor against ``target`` and the hostiles
    in range (``survival.at_health_floor``): no step off it is taken, or
    Retreat walks back and the step off follows, round after round (A114)."""
    return on_safe_tile(world) and at_health_floor(world, ctx.params, combat_group(world, ctx.policy, also=target))


def op_refusal(world: WorldModel, ctx: PlayContext, op: dict) -> str | None:
    """Why Fight cannot carry out the ``fight`` op, or None to go on: its NPC
    is not in view (and was not since the op reached the top, or it would be
    done), is a boss (``fight_boss``'s), the directives forbid attacking
    it, we stand on safe ground at the health floor
    (``held_on_safe_ground``), or no swing or step can reach it
    (``can_engage``), so the planner makes the call. Reads only; ``act``
    drops the op with it."""
    target = op_npc(op, world)
    if target is None:
        named = f"npc {op['npc_id']}" if "npc_id" in op else f"no {op.get('npc_type')}"
        return f"{named} in view to fight"
    if target.is_boss:
        return f"{target.code or 'npc'} {target.id} is a boss: fight_boss fights it"
    if attack_forbidden(target, ctx.never_attack):
        return f"the directives forbid attacking {target.code or 'npc'} {target.id}"
    if held_on_safe_ground(world, ctx, target):
        return f"at the health floor on safe ground: not stepping off toward {target.code or 'npc'} {target.id}"
    if not can_engage(world, target, ctx):
        return f"no swing or open step reaches {target.code or 'npc'} {target.id}"
    return None


def should_fight(world: WorldModel, ctx: PlayContext) -> bool:
    """The engagement's decision is to fight (``engagement.fights``) and
    we can swing or close in; false when we cannot close, so **Flee** runs."""
    policy = ctx.policy
    if policy.kind != "scripted" or not world.alive or world.pos is None:
        return False
    if policy.on_hostile != "fight" or on_safe_tile(world):
        return False
    target = fight_target(world, policy, ctx.never_attack, ctx.memory.engagement)
    if target is None or not fights(ctx.memory):
        return False
    return can_engage(world, target, ctx)


class FightState(State):
    """Priority 2, before **Flee**. Swings or closes on the nearest allowed hostile
    in range, else on the nearest member of the fight still in view (A113),
    when ``on_hostile = fight``, the engagement's decision is to fight
    (``engagement.sync_engagement``, the decision Flee and Retreat read too), and
    we are not on a safe tile; queues retreat steps behind ``Use`` when a safe
    tile is known."""

    name = "Fight"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return should_fight(world, ctx) or self._has_op(world, ctx)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    @staticmethod
    def _has_op(world: WorldModel, ctx: PlayContext) -> bool:
        """The plan's top op is a ``fight`` (reads only: ``act`` drops it when it cannot go on)."""
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, FightState.name) is not None

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name) if self._has_op(world, ctx) else None
        if op is not None:
            refusal = op_refusal(world, ctx, op)
            if refusal is None:
                return engage(world, ctx, op_npc(op, world), self.name)
            if ctx.plan is not None:
                ctx.plan.drop_current(refusal, memory=ctx.memory)
                return StateOutcome(None, f"fight dropped: {refusal}", state=self.name)
        target = fight_target(world, ctx.policy, ctx.never_attack, ctx.memory.engagement)
        if target is None or world.pos is None:
            ctx.memory.path = []
            return StateOutcome(None, "no target", state=self.name)
        return engage(world, ctx, target, self.name)


def engage(w: WorldModel, ctx: PlayContext, target: Entity, state: str) -> StateOutcome:
    """Swing at ``target`` with a retreat tail queued behind, or step closer when out of weapon reach.

    **Fight** runs it, and so does **Flee** when running away is not working (A9).
    No swing is sent from a safe tile, where no attack can be made: the move
    there is the step off it toward ``target`` (A114), never taken at the
    health floor (``held_on_safe_ground``).
    """
    m, policy = ctx.memory, ctx.policy
    m.path = []
    if on_safe_tile(w):
        if held_on_safe_ground(w, ctx, target):
            return StateOutcome(None, "at the health floor: staying on safe ground", state=state)
        blocked, _, _ = plan_sets(w, m, policy, ctx.knowledge)
        off = off_safe_step(w, target, blocked, weapon_reach(w, ctx.knowledge))
        if off is None:
            return StateOutcome(None, "no step off safe ground toward it", state=state)
        return StateOutcome([set_position(off)], f"off safe ground toward {target.kind} {target.id}", reflex=True, state=state)
    if not in_weapon_reach(w, target, ctx.knowledge):
        blocked, _, _ = plan_sets(w, m, policy, ctx.knowledge)
        toward = close_step(w, target, blocked)
        if toward is None:
            return StateOutcome(None, "cannot close", state=state)
        return StateOutcome(
            [set_position(toward)],
            f"close on {target.kind} {target.id}",
            reflex=True,
            state=state,
        )
    uses = [attack_intent(target) for _ in range(ATTACK_USES)]
    retreat = retreat_tail(w, m, policy, ctx, limit=8)
    horizon = queue_horizon_intents()
    queue = build_attack_queue(
        uses,
        retreat,
        poll_interval_ticks=m.calm_poll_interval,
        horizon_ticks=horizon,
        ticks_since_last_use=_ticks_since_use(w, m.last_use_tick),
    )
    if not queue:
        return StateOutcome(None, "attack queue empty", state=state)
    label = f"fight {target.kind} {target.id}"
    return StateOutcome(queue, label, reflex=True, state=state, paced=True)
