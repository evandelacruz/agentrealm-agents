"""Boss: plan preconditions, the fight clock, defeat from boss health (A38)."""

from __future__ import annotations

from ..config import Policy
from ..directives import attack_forbidden
from ..executor import build_attack_queue, queue_horizon_intents
from ..healing import POTION_CODES
from ..memory import BossFight, Memory
from ..navigation import cost_path
from ..pathing import grid_params, guided_step, nav_search, next_step
from ..plan import GoalOp, Plan
from ..survival import should_retreat
from ..world import DOORS, Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .fight import (
    ATTACK_USES,
    attack_intent,
    can_engage,
    close_step,
    in_weapon_reach,
)
from .intents import set_position

DOOR_GOAL = "boss:door"
# Stop trying to leave once the fight clock is this close to expiring (10 s at 10 Hz).
CLOCK_COMMIT_TICKS = 100


def boss_entity(w: WorldModel, fight: BossFight | None = None) -> Entity | None:
    """The boss in view: the one already fought, else the nearest (``Entity.is_boss``)."""
    bosses = [e for e in w.entities if e.is_boss]
    if fight is not None:
        bosses = [e for e in bosses if e.id == fight.boss_id]
    if not bosses:
        return None
    here = w.pos
    if here is None:
        return min(bosses, key=lambda e: e.id)
    return min(bosses, key=lambda e: (chebyshev(e.pos, here), e.id))


def current_fight_boss(plan: Plan | None) -> GoalOp | None:
    op = plan.current() if plan is not None else None
    if op is not None and op["op"] == "fight_boss":
        return op
    return None


def boss_fight_on(w: WorldModel, m: Memory) -> bool:
    """A boss is engaged, or the served clock runs: Retreat and Flee stand down."""
    return m.boss is not None or w.in_boss_fight()


def boss_defeated(w: WorldModel, fight: BossFight) -> bool:
    """Observed defeat only: the boss read at ``health <= 0``, or a level clear since it was seen.

    A boss out of view is not a defeat (API Reads, Round Trip).
    """
    for e in w.entities:
        if e.is_boss and e.id == fight.boss_id and e.health is not None and e.health <= 0:
            return True
    return w.level_clear_tick is not None and w.level_clear_tick >= fight.since_tick


def sync_boss(w: WorldModel, m: Memory, plan: Plan | None) -> None:
    """Start, end and finish the boss fight once per decision (A38).

    The one place ``m.boss`` changes. A fight starts when a boss is seen while
    a ``fight_boss`` op is current. It ends on death, when that op is no longer
    current (popped, dropped, or the stack reloaded), or when the boss is out
    of view with no served clock running. On an observed defeat Boss pops the
    op itself.
    """
    op = current_fight_boss(plan)
    if m.boss is not None and (m.boss.op is not op or not w.alive):
        m.boss = None
    if op is None or not w.alive:
        return
    if m.boss is None:
        boss = boss_entity(w)
        if boss is None:
            return
        m.boss = BossFight(op, boss.id, w.tick)
    if boss_defeated(w, m.boss):
        assert plan is not None
        plan.finish_current(f"boss {m.boss.boss_id} defeated")
        m.boss = None
        return
    if boss_entity(w, m.boss) is None and not w.in_boss_fight():
        m.boss = None


def potion_count(w: WorldModel) -> int:
    codes = POTION_CODES
    n = sum(1 for h in w.held_supplies if h.code in codes)
    n += sum(1 for s in w.chest_supplies if s.code in codes)
    return n


def fight_boss_preconditions_met(w: WorldModel, op: GoalOp) -> tuple[bool, str]:
    """Optional ``fight_boss`` fields from the plan (A38)."""
    if "min_health" in op:
        need = op["min_health"]
        health = w.health if w.health is not None else 0
        if health < need:
            return False, f"health {health} < {need}"
    if "min_potions" in op:
        have = potion_count(w)
        need = op["min_potions"]
        if have < need:
            return False, f"potions {have} < {need}"
    if "armed" in op:
        if w.armed_code != op["armed"]:
            return False, f"armed {w.armed_code!r} != {op['armed']!r}"
    if "worn" in op:
        worn = set(w.worn_codes.values())
        missing = [c for c in op["worn"] if c not in worn]
        if missing:
            return False, f"missing worn {missing}"
    return True, "ok"


def boss_door_pos(op: GoalOp) -> Pos:
    return (op["x"], op["y"])


def at_boss_door(w: WorldModel, door: Pos) -> bool:
    if w.pos is None:
        return False
    if w.pos == door:
        return True
    return chebyshev(w.pos, door) == 1 and w.view.tiles.get(door) in DOORS


def on_boss_door(w: WorldModel, door: Pos) -> bool:
    return w.pos == door and w.view.tiles.get(door) in DOORS


def can_retreat_out(w: WorldModel, ctx: PlayContext, door: Pos) -> bool:
    """True when a step toward the boss door is open (retreat-out when possible)."""
    if w.pos is None or w.map_id is None:
        return False
    if on_boss_door(w, door):
        return True
    m = ctx.memory
    blocked, _, _ = plan_sets(w, m, ctx.policy, ctx.knowledge)
    params = grid_params(ctx.policy, blocked, set(), allow_goal_door=True, m=m)
    path = cost_path(w, door, params, nav=nav_search(m, w, DOOR_GOAL, door)) or []
    return next_step(w, blocked, path) is not None


def should_commit(w: WorldModel, ctx: PlayContext, door: Pos) -> bool:
    """No retreat-out path, or the fight clock is nearly gone."""
    left = w.boss_fight_ticks_left()
    if left is not None and left <= CLOCK_COMMIT_TICKS:
        return True
    return not can_retreat_out(w, ctx, door)


def boss_attack_queue(w: WorldModel, m: Memory, target: Entity) -> list[dict] | None:
    uses = [attack_intent(target) for _ in range(ATTACK_USES)]
    horizon = queue_horizon_intents()
    queue = build_attack_queue(
        uses,
        [],
        poll_interval_ticks=m.calm_poll_interval,
        horizon_ticks=horizon,
        ticks_since_last_use=max(1, w.tick - m.last_use_tick) if m.last_use_tick is not None else None,
    )
    return queue or None


class BossState(State):
    """Priority 5, after Travel and before Level. Runs on ``fight_boss`` while preconditions hold or during a fight."""

    name = "Boss"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if boss_fight_on(world, ctx.memory):
            return True
        op = current_fight_boss(ctx.plan)
        if op is None:
            return False
        ok, _ = fight_boss_preconditions_met(world, op)
        return ok

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        if boss_fight_on(world, ctx.memory):
            return False
        op = current_fight_boss(ctx.plan)
        if op is None:
            return True
        ok, _ = fight_boss_preconditions_met(world, op)
        return not ok

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        op = current_fight_boss(ctx.plan)
        if op is None and m.boss is None:
            return StateOutcome(None, "no fight_boss op", state=self.name)
        _, plan_avoid, plan_costly = plan_sets(world, m, policy, ctx.knowledge)

        if m.boss is not None:
            boss = boss_entity(world, m.boss)
            if boss is None:
                return StateOutcome(None, "boss out of view", state=self.name)
            if attack_forbidden(boss, ctx.never_attack):
                return StateOutcome(None, "boss forbidden", state=self.name)
            door = boss_door_pos(m.boss.op)
            if (
                should_retreat(world, policy, ctx.params)
                and not should_commit(world, ctx, door)
            ):
                return _walk_to_door(world, m, policy, door, plan_avoid, plan_costly, "retreat out")
            if not can_engage(world, boss, ctx):
                return StateOutcome(None, "cannot reach boss", state=self.name)
            if not in_weapon_reach(world, boss, ctx.knowledge):
                step = close_step(world, boss, plan_avoid)
                if step is None:
                    return StateOutcome(None, "cannot close on boss", state=self.name)
                m.path = []
                return StateOutcome([set_position(step)], f"boss close on {boss.id}", reflex=True, state=self.name)
            queue = boss_attack_queue(world, m, boss)
            if queue is None:
                return StateOutcome(None, "boss attack empty", state=self.name)
            label = f"boss fight {boss.id}"
            if boss.health is not None and boss.max_health:
                label += f" ({boss.health}/{boss.max_health})"
            if world.boss_fight_ticks_left() is not None:
                label += f" clock {world.boss_fight_ticks_left()}t"
            return StateOutcome(queue, label, reflex=True, state=self.name, paced=True)

        if op is None:
            # The served clock runs but no boss is engaged: nothing to fight.
            return StateOutcome(None, "boss clock, no boss in view", state=self.name)
        door = boss_door_pos(op)
        if world.pos == door:
            return StateOutcome(None, "entering boss room", wait=True, state=self.name)
        if not at_boss_door(world, door):
            return _walk_to_door(world, m, policy, door, plan_avoid, plan_costly, "approach door")
        return StateOutcome([set_position(door)], "boss enter door", state=self.name)


def _walk_to_door(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    door: Pos,
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
    label: str,
) -> StateOutcome:
    def plan_fn(_att):
        params = grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m)
        return cost_path(w, door, params, nav=nav_search(m, w, DOOR_GOAL, door))

    step = guided_step(m, w, DOOR_GOAL, door, plan_avoid, plan_fn)
    if step is None:
        return StateOutcome(None, f"{label}: no path", state=BossState.name)
    return StateOutcome([set_position(step)], f"boss {label} → {door}", state=BossState.name)
