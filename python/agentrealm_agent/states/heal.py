"""Heal: how a hurt character heals (A10), and the weapon put back after a
``Use`` on yourself (A24).

Heal decides only how to heal. Whether healing is worth leaving the plan's
op for is the planner's: its ``low_health`` param sets the line below which
resting on safe ground goes ahead of the op and a carried potion is spent
(``healing.health_low``, ``healing.spend_potion``). What is sent after a
refusal is the executor's (``executor.supply_refusals``); Heal asks only
whether a supply may be tried now (``can_try``).
"""

from __future__ import annotations

from ..break_memory import choice_at, given_up_blocks
from ..config import Policy
from ..executor.intents import wait
from ..executor.supply_refusals import can_try, must_stand_on
from ..healing import (
    carried_heal,
    health_low,
    hurt,
    known_safe_cells,
    note_regen_sample,
    potion_count,
    regen_known,
    regen_per_tick,
    save_regen_yes,
    standing_in_safe_zone,
)
from ..hostile_ground import ground_by_hostile, reach_cells
from ..memory import Memory, queue_signal
from ..navigation import cost_path, path_cost
from ..navigation.planner import CostGridParams
from ..navigation import stuck as nav_stuck
from ..navigation.rejection import navigation_avoid_costly
from .. import targets as targets_mod
from ..pathing import (
    HEAL_TARGET,
    SAFE_WALK_GOALS,
    bounded_step,
    commit_safe,
    committed_safe,
    grid_params,
    nav_search,
    reachable_safe_goal,
    safe_ruled_out,
)
from ..supplies import is_food
from ..survival import hostiles_in_range, hostiles_reaching, town_cell
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, top_op
from .break_state import break_toward, in_break_reach
from .gather_safe import hostiles_near
from .intents import set_position, take
from .use_item import rearm_after_use, use_on_self

# A reflex takes only food close by: walking further for food is a planner
# op (``fetch_item``), not something Heal starts on its own.
FOOD_REACH = 3


class HealState(State):
    """Reflex, above Fight. Hurt and no hostile in range, it heals by the
    first of these that can be had:

    1. Food lying within ``FOOD_REACH``, the cheapest by its walk on Heal's
       ground (``_food_target``).
    2. Carried food, then a carried potion when ``healing.spend_potion``
       says it is worth drinking now (A96); the reserve is kept otherwise.
    3. Rest on safe ground, sampling safe-zone regen. Only when health is
       low (the planner's ``low_health``) or no plan op is on top: above the
       line the op goes first. On safe ground Heal walks to the zone's own
       unexplored edge, never out of it, and with none left sends ``Wait``
       and starts no walk. Once this run has measured no regen, Heal asks the
       planner once for food and potions (a ``heal_supplies`` trigger,
       counting the potions held back) and sends nothing.

    Every walk is priced the one way (``_walk_toward``) and bounded by stuck
    detection (``bounded_step``): one that goes nowhere gives its target up
    in the one give-up memory (A90). The walk to safe ground commits to one
    safe cell (A71); while a walk of Heal's to safe ground on this map is
    given up and backs off, safe ground is given up and the planner asked
    for supplies.

    A ``Use`` on yourself is ``Arm`` and ``Use`` in one paced queue. Heal
    runs on the next decision after it, even at full health or with a
    hostile in range, to re-arm the weapon it swapped out (A24): one ``Arm``,
    sent once, before anything else, so ``self_use_rearm`` never outlives
    the ``Use``."""

    name = "Heal"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return _wants_heal(world, ctx) or ctx.memory.self_use_rearm is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        # A Use on yourself went out: the weapon goes back before anything else.
        if ctx.memory.self_use_rearm is not None and (out := _rearm_weapon(world, ctx.memory)).intents:
            return out
        if not _wants_heal(world, ctx):
            return _out(None, "not hurt, or a hostile in range")
        return _choose(world, ctx)


def _wants_heal(w: WorldModel, ctx: PlayContext) -> bool:
    """Hurt and out of combat."""
    return hurt(w) and not hostiles_in_range(w, ctx.policy)


def _choose(w: WorldModel, ctx: PlayContext) -> StateOutcome:
    m = ctx.memory

    # Health changes from food or a potion must not count as regen.
    if out := _act_food(w, ctx):
        m.heal_regen_sample = None
        return out
    if out := use_carried_heal(w, ctx):
        m.heal_regen_sample = None
        return out

    known = regen_known(ctx.knowledge, m)
    if known == "no":
        _ask_for_supplies(w, m)
        return _out(None, "no safe-zone regen this run")
    if (op := top_op(ctx)) is not None and not health_low(w, ctx.params):
        m.heal_regen_sample = None
        return _out(None, f"health above low_health: the plan's {op['op']} goes first")
    if not standing_in_safe_zone(w):
        m.heal_regen_sample = None
        if _safe_given_up(m, w):
            _ask_for_supplies(w, m, safe_ground="unreachable")
            return _out(None, "safe ground given up while its walk backs off")
        goal = "heal_rest" if known == "yes" else "heal_measure"
        if out := _walk_to_safe(w, ctx, goal=goal):
            return out
        if committed_safe(m, w, HEAL_TARGET) is None:
            # No safe cell a walk reaches, or the one committed to was given
            # up: tell the planner. A step held this decision keeps the cell.
            _ask_for_supplies(w, m, safe_ground="unreachable")
        return _out(None, "no reachable safe tile")
    # A "yes" saved with no rate is measured again on the next rest, so the
    # planner's rest estimate (A124) comes to every world that has regen.
    if known is None or regen_per_tick(ctx.knowledge) is None:
        verdict = note_regen_sample(m, w)
        if verdict == "yes":
            save_regen_yes(ctx.knowledge, m.heal_regen_per_tick)
        elif verdict == "no" and known is None:
            m.heal_regen_absent = True  # this run only: never saved (one noisy window)
            _ask_for_supplies(w, m)
            return _out(None, "no safe-zone regen this run")
    return _explore_zone(w, ctx)


def _explore_zone(w: WorldModel, ctx: PlayContext) -> StateOutcome:
    """Resting in a safe zone: walk to the nearest unexplored edge of the zone
    itself, or ``Wait`` when none is left.

    Never the safe default: its frontiers lie outside the zone, and leaving
    cuts the regen sample short and walks straight back. A rest with nothing
    to explore starts no walk and no stuck attempt, so a long heal leaves
    stuck detection as it was.
    """
    m, policy = ctx.memory, ctx.policy
    here = w.pos
    assert here is not None
    blocked, _ = _ground(w, m, policy, ctx)
    goal = "heal_explore"
    frontier = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier() - {here}, w.tick)
    edge = sorted(
        (chebyshev(here, p), p)
        for p in frontier
        if standing_in_safe_zone(w, p) and p not in blocked and not hostiles_near(w, p, policy)
    )
    if edge:
        # Off-zone cells are walls for this walk, so it never steps out of the zone.
        outside = {p for p in w.view.tiles if not standing_in_safe_zone(w, p)}
        cells = {p for _, p in edge}
        # The edge cell is committed until revealed or given up (A71).
        target = targets_mod.hold(
            m, w, goal, lambda: (w.map_id, edge[0][1]), lambda t: t[0] == w.map_id and t[1] in cells
        )[1]
        if out := _walk_toward(w, ctx, target, goal=goal, avoid=outside):
            out.reason = f"heal in safe ground: {out.reason}"
            return out
    return _out([wait()], "heal: rest in the safe zone")


def _ask_for_supplies(w: WorldModel, m: Memory, *, safe_ground: str | None = None) -> None:
    """Hurt, nothing to eat and no potion due (``healing.spend_potion``), and
    no regen, or (``safe_ground``) no safe ground to rest on: ask the planner
    for food and potions (a ``fetch_item`` or ``buy``), once until health is
    full again (``note_heal_window`` re-arms it). The ask carries the potions
    held: a reserve held back above the low line, which the planner weighs
    before buying more (A96)."""
    if m.heal_supplies_asked:
        return
    m.heal_supplies_asked = True
    signal = {
        "trigger": "heal_supplies",
        "health": w.health,
        "max_health": w.max_health,
        "potions": potion_count(w),
        "tick": w.tick,
    }
    if safe_ground is None:
        signal["regen"] = "no"
    else:
        signal["safe_ground"] = safe_ground
    queue_signal(m, signal)


def _out(intents: list[dict] | None, reason: str) -> StateOutcome:
    return StateOutcome(intents, reason, state=HealState.name)


def _ground(
    w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, reach: set[Pos] | None = None
) -> tuple[set[Pos], set[Pos]]:
    """Heal's ground, as (avoid, costly), for every walk and pick it makes:
    hazards are both, and cells in a known hostile's reach, in view or
    remembered (``reach``, ``hostile_ground.reach_cells`` when not given),
    are costly, so a walk goes round a pack instead of through it."""
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    if reach is None:
        reach = reach_cells(w, policy)
    return nav_avoid | hazards, nav_costly | hazards | reach


def _walk_to_safe(w: WorldModel, ctx: PlayContext, *, goal: str) -> StateOutcome | None:
    """A step toward the one safe cell this walk commits to, or None.

    The cell is picked as Retreat and Park pick theirs
    (``pathing.reachable_safe_goal``): the first known safe cell a path
    reaches on Heal's ground (near town first, then nearest), else the town
    cell, skipping cells walled in, ruled out, or in a known hostile's
    ground, in view or remembered (``hostile_ground.ground_by_hostile``). It
    is committed (``pathing.HEAL_TARGET``, A71) and kept while it stays
    valid, even when another safe cell comes nearer.

    A walk that gives it up (no path, no progress in a stuck window, or
    pacing), or a check that rules it out, gives it up in the one give-up
    memory (A90), and with it safe ground while that backs off
    (``_safe_given_up``): Heal never moves on to the next safe cell at once,
    so cells it cannot reach cannot chain into a loop. A cell a hostile now
    holds is no give-up: the next one is taken.
    """
    m, policy = ctx.memory, ctx.policy
    reach = ground_by_hostile(w, policy)
    avoid, costly = _ground(w, m, policy, ctx, set().union(*reach.values()))
    params = grid_params(policy, avoid, costly)
    kept = committed_safe(m, w, HEAL_TARGET)
    target = reachable_safe_goal(m, w, known_safe_cells(w), params, town_cell(w, ctx.knowledge), reach, prefer=kept)
    if kept is not None and target != kept and safe_ruled_out(m, w, kept):
        _give_up_safe(m, w, goal, kept)  # the one cell it committed to is out of reach: no second pick
        return None
    commit_safe(m, w, HEAL_TARGET, target)
    if target is None:
        return None
    if out := _walk_toward(w, ctx, target, goal=goal):
        return out
    if nav_stuck.backed_off(m, goal, w.map_id, target, w.tick):
        commit_safe(m, w, HEAL_TARGET, None)  # the walk gave it up
    return None  # else no step open this decision: the cell stays committed


def _give_up_safe(m: Memory, w: WorldModel, goal: str, cell: Pos) -> None:
    """Give the walk to ``cell`` up, as stuck detection gives up any walk
    (``stuck.give_up``): it backs off, the planner hears of it, and safe
    ground is given up while it does."""
    if (att := nav_stuck.attempt(m, w, goal, cell)) is not None:
        nav_stuck.give_up(m, w, att, "no_path")
    commit_safe(m, w, HEAL_TARGET, None)


def _safe_given_up(m: Memory, w: WorldModel) -> bool:
    """A walk of Heal's to a safe cell on this map was given up and still
    backs off (A90). The backoff doubles with each give-up there, and when to
    send the character back is the planner's call (the ``stuck`` trigger's
    ``retry_at_tick``)."""
    return any(
        r.goal in SAFE_WALK_GOALS and r.map_id == w.map_id and w.tick < r.until for r in m.nav_stuck.given_up.values()
    )


def _walk_grids(w: WorldModel, ctx: PlayContext, at: Pos, avoid: set[Pos]) -> tuple[CostGridParams, CostGridParams]:
    """Heal's ground for a walk to ``at`` (``avoid`` added), as the grid it
    steps on and the same grid with each block on the straight way a held
    item opens priced at its break (``grid_params`` ``break_goal``)."""
    m, policy = ctx.memory, ctx.policy
    plan_avoid, plan_costly = _ground(w, m, policy, ctx)
    plan_avoid = plan_avoid | avoid
    walk = grid_params(policy, plan_avoid, plan_costly)
    opening = grid_params(policy, plan_avoid, plan_costly, m=m, w=w, knowledge=ctx.knowledge, break_goal=at)
    return walk, opening


def _walk_toward(
    w: WorldModel, ctx: PlayContext, at: Pos, *, goal: str, avoid: set[Pos] = frozenset()
) -> StateOutcome | None:
    """One move toward ``at``, or None when there is none now.

    One price for every Heal walk: its walk on Heal's ground (``_ground``),
    a block on the straight way a held item opens costed at its break. When
    the cheapest way crosses such a block, Heal walks that way on its own
    ground and opens the block once it is in the opener's reach
    (``break_toward``); else it steps on the plain grid. Either walk is
    bounded like any other (``bounded_step``, A15): no path, or no progress
    in its window, gives ``at`` up with a backoff, so Heal tries something
    else instead of pacing. A kept path that now crosses the reach of a known
    hostile it did not cross when planned is planned again on the grid that
    prices it.
    """
    m, policy = ctx.memory, ctx.policy
    plain, opening = _walk_grids(w, ctx, at, avoid)
    way = cost_path(w, at, opening) if opening.break_costs else None
    grid = plain
    if (cut := next((p for p in way or [] if p in opening.break_costs), None)) is not None:
        # The first block the priced way crosses is the one opened, so the
        # walk and its price never part.
        choice = choice_at(w, ctx.knowledge, cut, given_up_blocks(m.nav_stuck, w.map_id, w.tick))
        if choice is not None:
            if in_break_reach(w, ctx, choice):
                out = break_toward(w, ctx, choice, HealState.name)
                out.reason = f"{goal}: {out.reason}"
                return out
            grid = opening  # out of reach: walk the priced way on Heal's ground
    if m.goal == goal and hostiles_reaching(w, policy, m.path) - m.planned_threats:
        m.path = []

    def plan() -> list[Pos] | None:
        return cost_path(w, at, grid, nav=nav_search(m, w, goal, at))

    step = bounded_step(m, w, goal, at, grid.avoid, plan, params=lambda: grid)
    # What the path crosses now it could not go round: only a hostile beyond it replans.
    m.planned_threats = hostiles_reaching(w, policy, m.path)
    return _out([set_position(step)], f"{goal} → {at}") if step is not None else None


def _food_target(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The food Heal goes for: of the food lying within ``FOOD_REACH`` that
    may be taken now (``supply_refusals.can_try``), the cheapest by its walk
    (``_walk_toward``'s price; food in reach costs none). The food it went
    for first stays its target while it is one (A71)."""
    m = ctx.memory
    here = w.pos
    assert here is not None
    foods = [
        e
        for e in w.entities
        if e.kind == "supply"
        and is_food(e.code)
        and chebyshev(e.pos, here) <= FOOD_REACH
        and can_try(m, w, "take", e.id, e.pos)
        and not nav_stuck.backed_off(m, "heal_food", w.map_id, e.pos, w.tick)
    ]
    c = targets_mod.committed(m, w, "heal_food")
    if (kept := next((f for f in foods if c is not None and f.id == c.target), None)) is not None:
        return kept
    targets_mod.release(m, "heal_food")

    def price(f: Entity) -> tuple[int, int, int] | None:
        if chebyshev(f.pos, here) <= _take_reach(m, f):
            return (0, 0, f.id)
        _, opening = _walk_grids(w, ctx, f.pos, set())
        way = cost_path(w, f.pos, opening)
        if not way or way[-1] != f.pos:
            return None
        cost = path_cost(w, way, f.pos, opening)
        return None if cost is None else (cost, chebyshev(f.pos, here), f.id)

    priced = [(p, f) for f in foods if (p := price(f)) is not None]
    return min(priced, key=lambda pf: pf[0])[1] if priced else None


def _take_reach(m: Memory, food: Entity) -> int:
    """How near a ``Take`` of ``food`` is sent from: beside it, or on its own
    cell once a ``Take`` from beside it was refused out of reach."""
    return 0 if must_stand_on(m, food) else 1


def _act_food(w: WorldModel, ctx: PlayContext) -> StateOutcome | None:
    m = ctx.memory
    here = w.pos
    assert here is not None
    food = _food_target(w, ctx)
    if food is None:
        return None
    if chebyshev(food.pos, here) <= _take_reach(m, food):
        nav_stuck.finish_in_reach(m, w, "heal_food")
        return _out([take(food.id)], f"take food {food.code}")
    # Walking onto it also picks up food eaten on pickup.
    if out := _walk_toward(w, ctx, food.pos, goal="heal_food"):
        targets_mod.commit(m, w, "heal_food", food.id)
        return out
    return None


def use_carried_heal(w: WorldModel, ctx: PlayContext) -> StateOutcome | None:
    """``Use`` on yourself carried food, or a potion ``healing.spend_potion``
    says is worth drinking now (health low, or the drink turns the fight,
    A96), by the one Use-self path (``use_item.use_on_self``, A108). Retreat
    runs it too, when it is losing ground (A9).

    A supply the server refused, or whose ``Use`` did nothing, is skipped
    until what it was refused for no longer holds
    (``executor.supply_refusals``, A80, A76). The weapon to put back is
    re-armed on Heal's next decision (``_rearm_weapon``)."""
    item = carried_heal(w, ctx.memory, ctx.policy, ctx.params)
    if item is None:
        return None
    return use_on_self(w, ctx.memory, item, HealState.name)


def _rearm_weapon(w: WorldModel, m: Memory) -> StateOutcome:
    """Re-``Arm`` the weapon a ``Use`` on yourself swapped out (A24). Sends
    nothing when it is already armed or no longer in hand."""
    code = m.self_use_rearm
    if intents := rearm_after_use(w, m):
        return _out(intents, f"re-arm {code}")
    return _out(None, f"no re-arm needed for {code}")
