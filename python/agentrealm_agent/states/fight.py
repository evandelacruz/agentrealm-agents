"""Fight: win-estimate gated combat with retreat queued behind attacks (A23)."""

from __future__ import annotations

from ..directives import attack_forbidden
from ..executor import build_attack_queue, queue_horizon_intents
from ..executor.intents import step
from ..executor.movement import direction_between
from ..knowledge_base import KnowledgeBase
from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step
from ..survival import nearest_safe_goal, on_safe_tile, would_lose
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .intents import set_position, use_npc, use_on


# ``Use`` swings queued per submit, ahead of the retreat tail.
ATTACK_USES = 3


def fight_target(w: WorldModel, policy, never_attack: list[str]) -> Entity | None:
    """Nearest hostile in range that ``on_hostile = fight`` may swing at."""
    here = w.pos
    if here is None or policy.on_hostile != "fight":
        return None
    hostiles = [
        e
        for e in w.entities
        if e.kind in policy.hostile
        and not (e.kind == "npc" and e.health is not None)
        and chebyshev(e.pos, here) <= policy.hostile_range
    ]
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


def weapon_reach(w: WorldModel, knowledge: KnowledgeBase | None) -> int:
    if w.attack_range is not None:
        return w.attack_range
    code = w.armed_code
    if code and knowledge and knowledge.items:
        learned = knowledge.items.get(code, {}).get("attack_range")
        if learned is not None:
            return int(learned)
    return 1


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
    """``Step`` intents toward the nearest safe tile, up to ``limit``."""
    goal = nearest_safe_goal(w)
    if goal is None or w.pos is None or w.pos == goal:
        return []
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
    params = grid_params(policy, plan_avoid, plan_costly)
    if m.goal != "safe" or not m.path or m.path[-1] != goal:
        m.path = cost_path(w, goal, params, nav=nav_search(m, w, "safe", goal)) or []
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


def can_engage(world: WorldModel, target: Entity, ctx: PlayContext) -> bool:
    """In weapon reach, or one open step brings us closer."""
    if in_weapon_reach(world, target, ctx.knowledge):
        return True
    blocked, _, _ = plan_sets(world, ctx.memory, ctx.policy, ctx.knowledge)
    return close_step(world, target, blocked) is not None


def should_fight(world: WorldModel, ctx: PlayContext) -> bool:
    """Swing or close in; false when we cannot close, so **Flee** runs."""
    policy = ctx.policy
    if policy.kind != "scripted" or not world.alive or world.pos is None:
        return False
    if policy.on_hostile != "fight" or on_safe_tile(world):
        return False
    target = fight_target(world, policy, ctx.never_attack)
    if target is None or would_lose(world, policy, ctx.params):
        return False
    return can_engage(world, target, ctx)


class FightState(State):
    name = "Fight"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return should_fight(world, ctx)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not should_fight(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        m.path = []
        target = fight_target(w, policy, ctx.never_attack)
        if target is None or w.pos is None:
            return StateOutcome(None, "no target", state=self.name)
        if not in_weapon_reach(w, target, ctx.knowledge):
            blocked, _, _ = plan_sets(w, m, policy, ctx.knowledge)
            toward = close_step(w, target, blocked)
            if toward is None:
                return StateOutcome(None, "cannot close", state=self.name)
            m.path = []
            return StateOutcome(
                [set_position(toward)],
                f"close on {target.kind} {target.id}",
                reflex=True,
                state=self.name,
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
            return StateOutcome(None, "attack queue empty", state=self.name)
        label = f"fight {target.kind} {target.id}"
        return StateOutcome(queue, label, reflex=True, state=self.name, paced=True)
