"""Boss: plan preconditions, the fight clock, defeat from boss health (A38).

The clock is counted, not read (A163, ``boss_clock``): it starts on the
step through the op's boss door, against the level's limit kept in the
knowledge base. Near the limit, or on a retreat, Boss walks out through
the room's exit door (``boss_clock.exit_door``); the boss keeps the damage
it has taken, and its served health prices a return (``survival.health_left``)."""

from __future__ import annotations

from agentrealm_adk import boss_clock
from agentrealm_adk.config import Policy
from agentrealm_adk.directives import attack_forbidden
from agentrealm_adk.executor import attack_cooldown_ticks, build_attack_queue, queue_horizon_intents, ticks_per_step
from agentrealm_adk.healing import potion_count
from agentrealm_adk.memory import BossFight, Memory
from agentrealm_adk.navigation import cost_path
from agentrealm_adk.reach import weapon_reach
from agentrealm_adk.pathing import grid_params, guided_step, nav_search, next_step, plan_sets
from agentrealm_adk.engagement import fights
from agentrealm_adk.survival import should_retreat
from agentrealm_adk.world import DOORS, Entity, Pos, WorldModel, chebyshev

from ..plan import GoalOp, Plan
from .base import PlayContext, State, StateOutcome
from .fight import (
    ATTACK_USES,
    attack_intent,
    can_engage,
    close_step,
    in_weapon_reach,
)
from .intents import set_position
from .way import clear_the_way

DOOR_GOAL = "boss:door"
EXIT_GOAL = "boss:exit"
# Seconds kept in hand beyond the walk to the exit door: with less left than
# that walk and these, Boss leaves (A163).
LEAVE_MARGIN_SECONDS = 10


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
    """A boss is engaged, or the counted clock runs: Retreat and Flee stand down."""
    return m.boss is not None or boss_clock.running(m, w)


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

    The one place ``m.boss`` and ``m.boss_clock`` change. The clock starts on
    a step through the current ``fight_boss`` op's door, keeps the room's
    doors seen, and stops once the character is dead or off the room's map,
    or once no ``fight_boss`` op is current (A163). A fight starts when a
    boss is seen while a ``fight_boss`` op is current. It ends on death, when
    that op is no longer current (popped, dropped, or the stack reloaded), or
    when the boss is out of view with no clock running. On an observed
    defeat Boss pops the op itself.
    """
    op = current_fight_boss(plan)
    if op is not None and w.alive:
        boss_clock.start(m, w, boss_door_pos(op))
    if m.boss_clock is not None and (op is None or not boss_clock.running(m, w)):
        m.boss_clock = None
    boss_clock.note_doors(m, w)
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
        plan.finish_current(f"boss {m.boss.boss_id} defeated", memory=m)
        m.boss = m.boss_clock = None
        return
    if boss_entity(w, m.boss) is None and not boss_clock.running(m, w):
        m.boss = None


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


def exit_steps(w: WorldModel, ctx: PlayContext, target: Pos) -> int | None:
    """Steps of the walk to the way out, None with no step toward it open:
    priced once per position and target, not every decision (``BossClock.route``)."""
    c = ctx.memory.boss_clock
    if c is None or w.pos is None or w.map_id is None:
        return None
    key = (w.pos, target)
    if c.route is not None and c.route[0] == key:
        return c.route[1]
    m = ctx.memory
    blocked, _, _ = plan_sets(w, m, ctx.policy, ctx.knowledge)
    params = grid_params(ctx.policy, blocked, set(), allow_goal_door=True, m=m)
    path = cost_path(w, target, params, nav=nav_search(m, w, EXIT_GOAL, target)) or []
    c.route = (key, len(path) if next_step(w, blocked, path) is not None else None)
    return c.route[1]


def clock_nearly_out(w: WorldModel, ctx: PlayContext, steps: int) -> bool:
    """Less of the counted clock is left than the walk of ``steps`` to the
    way out and ``LEAVE_MARGIN_SECONDS`` (A163)."""
    left = boss_clock.ticks_left(ctx.memory, w, ctx.knowledge, ctx.tick_hz)
    if left is None:
        return False
    step = ticks_per_step(tick_rate_hz=ctx.tick_hz, movement_speed_milli=max(1, w.movement_speed))
    return left <= steps * step + LEAVE_MARGIN_SECONDS * ctx.tick_hz


def leave_room(
    w: WorldModel, ctx: PlayContext, plan_avoid: set[Pos], plan_costly: set[Pos], *, retreat: bool
) -> StateOutcome | None:
    """The walk out of the boss room (A163): to the exit door, else back to
    where the boss door landed (``boss_clock.way_out``), near the limit or
    on Retreat's rule; None to fight on: neither holds, or no step toward
    it is open."""
    m = ctx.memory
    target = boss_clock.way_out(m, w, ctx.knowledge)
    if target is None:
        return None
    steps = exit_steps(w, ctx, target)
    if steps is None:
        return None
    if clock_nearly_out(w, ctx, steps):
        return _walk_to_door(w, m, ctx.policy, target, plan_avoid, plan_costly, "leave before the limit", EXIT_GOAL)
    if retreat:
        return _walk_to_door(w, m, ctx.policy, target, plan_avoid, plan_costly, "retreat out", EXIT_GOAL)
    return None


def boss_attack_queue(w: WorldModel, m: Memory, target: Entity, tick_hz: int) -> list[dict] | None:
    uses = [attack_intent(target) for _ in range(ATTACK_USES)]
    horizon = queue_horizon_intents()
    queue = build_attack_queue(
        uses,
        [],
        poll_interval_ticks=m.calm_poll_interval,
        horizon_ticks=horizon,
        weapon_cooldown_ticks=attack_cooldown_ticks(w.armed_code, tick_hz),
        ticks_since_last_use=max(1, w.tick - m.last_use_tick) if m.last_use_tick is not None else None,
    )
    return queue or None


class BossState(State):
    """Priority 5, after Travel and before Level. Runs on ``fight_boss``, and
    during a fight. With the op's preconditions not met, no way to the door
    or the boss, or nothing to do this decision, it clears the way or asks
    the planner and holds (``way``, A120)."""

    name = "Boss"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return boss_fight_on(world, ctx.memory) or current_fight_boss(ctx.plan) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = current_fight_boss(ctx.plan)
        out = self._act(world, ctx, op)
        if op is None or out.intents or out.wait or ctx.plan is None or ctx.plan.current() is not op:
            return out
        # Nothing to do for the op this decision: ask the planner and hold (A120).
        return clear_the_way(world, ctx, None, out.reason, self.name, op)

    def _act(self, world: WorldModel, ctx: PlayContext, op: GoalOp | None) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        if op is None and m.boss is None:
            return StateOutcome(None, "no fight_boss op", state=self.name)
        _, plan_avoid, plan_costly = plan_sets(world, m, policy, ctx.knowledge)

        if m.boss is not None:
            boss = boss_entity(world, m.boss)
            if boss is None:
                return StateOutcome(None, "boss out of view", state=self.name)
            if attack_forbidden(boss, ctx.never_attack):
                return StateOutcome(None, "boss forbidden", state=self.name)
            out = leave_room(world, ctx, plan_avoid, plan_costly, retreat=should_retreat(world, policy, ctx.params, fights(m)))
            if out is not None:
                return out
            if not can_engage(world, boss, ctx):
                return clear_the_way(world, ctx, boss.pos, "cannot reach boss", self.name, op, reach=())
            if not in_weapon_reach(world, boss, ctx.knowledge):
                step = close_step(world, boss, plan_avoid, weapon_reach(world, ctx.knowledge))
                if step is None:
                    return clear_the_way(world, ctx, boss.pos, "cannot close on boss", self.name, op, reach=())
                m.path = []
                return StateOutcome([set_position(step)], f"boss close on {boss.id}", reflex=True, state=self.name)
            queue = boss_attack_queue(world, m, boss, ctx.tick_hz)
            if queue is None:
                return StateOutcome(None, "boss attack empty", state=self.name)
            label = f"boss fight {boss.id}"
            if boss.health is not None and boss.max_health:
                label += f" ({boss.health}/{boss.max_health})"
            left = boss_clock.ticks_left(m, world, ctx.knowledge, ctx.tick_hz)
            if left is not None:
                label += f" clock {left}t"
            return StateOutcome(queue, label, reflex=True, state=self.name, paced=True)

        if boss_clock.running(m, world):
            # In the boss room with no boss in view: out before the limit, else nothing to fight.
            out = leave_room(world, ctx, plan_avoid, plan_costly, retreat=False)
            if out is not None:
                return out
            return StateOutcome(None, "boss clock, no boss in view", state=self.name)
        if op is None:
            return StateOutcome(None, "no fight_boss op", state=self.name)
        ok, why = fight_boss_preconditions_met(world, op)
        if not ok:
            return StateOutcome(None, f"fight_boss preconditions not met: {why}", state=self.name)
        door = boss_door_pos(op)
        if world.pos == door:
            return StateOutcome(None, "entering boss room", wait=True, state=self.name)
        if not at_boss_door(world, door):
            out = _walk_to_door(world, m, policy, door, plan_avoid, plan_costly, "approach door")
            return out if out.intents else clear_the_way(world, ctx, door, out.reason, self.name, op, reach=())
        return StateOutcome([set_position(door)], "boss enter door", state=self.name)


def _walk_to_door(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    door: Pos,
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
    label: str,
    goal: str = DOOR_GOAL,
) -> StateOutcome:
    def params():
        return grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m)

    def plan_fn(_att):
        return cost_path(w, door, params(), nav=nav_search(m, w, goal, door))

    step = guided_step(m, w, goal, door, plan_avoid, plan_fn, params=params)
    if step is None:
        return StateOutcome(None, f"{label}: no path", state=BossState.name)
    return StateOutcome([set_position(step)], f"boss {label} → {door}", state=BossState.name)
