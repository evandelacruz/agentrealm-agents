"""Heal: food, carried food or potion, then safe ground (A10); re-arm the
weapon a drink swapped out (A24)."""

from __future__ import annotations

from ..break_memory import nominate_on_path
from ..config import Policy
from ..equip import weapon_to_rearm
from ..healing import (
    carried_heal,
    food_in_sight,
    health_low,
    hurt,
    known_safe_cells,
    must_arm,
    must_stand_on,
    note_regen_sample,
    potion_count,
    rearm_after_drink,
    regen_known,
    save_regen_yes,
    standing_in_safe_zone,
)
from ..hostile_ground import ground_by_hostile, reach_cells
from ..memory import Memory, queue_signal
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..navigation.rejection import navigation_avoid_costly
from .. import targets as targets_mod
from ..pathing import (
    HEAL_TARGET,
    bounded_step,
    commit_safe,
    committed_safe,
    grid_params,
    nav_search,
    SAFE_UNREACHABLE_TICKS,
    reachable_safe_goal,
    safe_ruled_out,
)
from ..survival import hostiles_in_range, hostiles_reaching, town_cell
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, top_op
from .break_state import break_toward
from .gather_safe import hostiles_near
from .intents import arm_and_use, set_position, take, use_self
from ..executor.intents import wait

# Food lying in sight that Heal tries to path to, nearest first.
FOOD_CANDIDATES = 3
# A reflex takes only food close by: walking further for food is a planner
# op (``fetch_item``), not something Heal starts on its own.
FOOD_REACH = 3


class HealState(State):
    """Reflex, above Fight. Hurt and out of combat: food within ``FOOD_REACH`` (cutting
    through a block the weapon opens when nothing else leads there), then carried
    food, then a carried potion when health is low (``healing.spend_potion``,
    A96: the reserve is kept otherwise), then safe ground. Every walk, to food or a safe tile, is
    bounded by stuck detection (``bounded_step``): one that goes nowhere gives
    its target up.

    In a safe zone Heal walks to the zone's own unexplored edge, never out
    of it, sampling regen as it goes; with none left it sends ``Wait`` and
    starts no walk, so the sample is never cut short by walking out and
    back, and a long rest leaves stuck detection untouched.
    Once this run has measured no regen, Heal never samples or walks to a
    safe zone again: it asks the planner once for food and potions (a
    ``heal_supplies`` trigger) and sends nothing, so the plan's executor or
    the safe default moves.

    Safe ground (the walk, the rest, the regen sample) outranks a plan op in
    progress only when health is low (``healing.health_low``): at 9/10 the
    plan's op goes first. The walk commits to one safe tile a path reaches;
    when it gives that tile up, Heal gives safe ground up until a full heal
    (at low health, for ``SAFE_UNREACHABLE_TICKS``), asks the planner for
    supplies, and yields (free-play run 9).

    A drink is ``Arm`` and ``Use`` in one paced queue. Heal runs on the next
    decision after it, even at full health or with a hostile in range, to
    re-arm the weapon the drink swapped out (A24): one ``Arm``, sent once,
    before anything else, so ``heal_rearm`` never outlives a drink."""

    name = "Heal"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return _wants_heal(world, ctx) or ctx.memory.heal_rearm is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        # A drink went out: the weapon goes back before anything else.
        if ctx.memory.heal_rearm is not None and (out := _rearm_weapon(world, ctx.memory)).intents:
            return out
        if not _wants_heal(world, ctx):
            return _out(None, "not hurt, or a hostile in range")
        return _choose(world, ctx)


def _wants_heal(w: WorldModel, ctx: PlayContext) -> bool:
    """Hurt and out of combat."""
    return hurt(w) and not hostiles_in_range(w, ctx.policy)


def _choose(w: WorldModel, ctx: PlayContext) -> StateOutcome:
    m, policy = ctx.memory, ctx.policy

    # Food lying in sight, then carried food, then a carried potion when
    # ``healing.spend_potion`` says so (the plan's Heal row): else rest. Health
    # changes from these must not count as regen.
    if out := _act_food(w, m, policy, ctx):
        m.heal_regen_sample = None
        return out
    if out := use_carried_heal(w, ctx):
        m.heal_regen_sample = None
        return out

    known = regen_known(ctx.knowledge, m)
    if known == "no":
        _ask_for_supplies(w, m)
        return _out(None, "no safe-zone regen this run")
    if (op := top_op(ctx)) is not None and not health_low(w):
        # Resting or measuring regen with health not low is worth less than
        # the plan's op (free-play run 9: a measure walk at 9/10 held off
        # ``travel`` for 217 s).
        m.heal_regen_sample = None
        return _out(None, f"health not low: the plan's {op['op']} goes first")
    if not standing_in_safe_zone(w):
        m.heal_regen_sample = None
        if _safe_given_up(m, w):
            _ask_for_supplies(w, m, safe_ground="unreachable")
            return _out(None, "safe ground given up for now")
        goal = "heal_rest" if known == "yes" else "heal_measure"
        if out := _walk_to_safe(w, m, policy, ctx, goal=goal):
            return out
        if _safe_given_up(m, w):
            _ask_for_supplies(w, m, safe_ground="unreachable")
            return _out(None, "safe tile given up: safe ground given up for now")
        if committed_safe(m, w, HEAL_TARGET) is None:
            # No safe cell a path reaches at all (every one walled in, held or
            # ruled out), not a step held this decision: tell the planner too.
            _ask_for_supplies(w, m, safe_ground="unreachable")
        return _out(None, "no reachable safe tile")
    if known is None:
        verdict = note_regen_sample(m, w)
        if verdict == "yes":
            save_regen_yes(ctx.knowledge)
        elif verdict == "no":
            m.heal_regen_absent = True  # this run only: never saved (one noisy window)
            _ask_for_supplies(w, m)
            return _out(None, "no safe-zone regen this run")
    return _explore_zone(w, m, policy, ctx)


def _explore_zone(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> StateOutcome:
    """Resting in a safe zone: walk to the nearest unexplored edge of the zone
    itself, or ``Wait`` when none is left.

    Never the safe default: its frontiers lie outside the zone, and leaving
    cuts the regen sample short and walks straight back (live flip-flop). A
    rest with nothing to explore starts no walk and no stuck attempt, so a
    long heal leaves stuck detection as it was.
    """
    here = w.pos
    assert here is not None
    blocked, _ = _plan_blocked(w, m, policy, ctx)
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
        if out := _walk_toward(w, m, policy, ctx, target, goal=goal, avoid=outside):
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


def _plan_blocked(
    w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, reach: set[Pos] | None = None
) -> tuple[set[Pos], set[Pos]]:
    """Heal's (avoid, costly): hazards are both; cells in a known hostile's
    reach, in view or remembered (``reach``, ``hostile_ground.reach_cells``
    when not given), are costly, so a walk to food or safe ground goes round
    a pack instead of through it (A63 run 4)."""
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    if reach is None:
        reach = reach_cells(w, policy)
    return nav_avoid | hazards, nav_costly | hazards | reach


def _walk_to_safe(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, *, goal: str) -> StateOutcome | None:
    """A step toward the one safe cell this walk commits to, or None.

    The cell is picked as Retreat and Park pick theirs
    (``pathing.reachable_safe_goal``): the first known safe cell a path
    reaches (near town first, then nearest), else the town cell, skipping
    cells proven walled in (``navigation.no_way``) or ruled out
    (``pathing.safe_ruled_out``) and those in a known hostile's ground, in
    view or remembered (``hostile_ground.ground_by_hostile``; free-play run
    2, A63 run 4). It is committed (``pathing.HEAL_TARGET``, A71) and kept
    while it stays valid, even when another safe cell comes nearer.

    A walk that gives it up (no path, no progress in a stuck window, or
    pacing), or a check that rules it out, marks it unreachable for every
    safe walk (``Memory.safe_unreachable``, as Retreat and Park do) and gives
    safe ground up (``Memory.heal_safe_given_up``): until a full heal, or,
    once health is low, for ``SAFE_UNREACHABLE_TICKS``. Heal never moves on
    to the next tile at once, so unreachable tiles cannot chain into a loop
    (free-play run 9: 217 s between safe tiles it could not reach). At low
    health one tile is tried again after each lapse, so a reachable tile is
    never shut out for good.
    A cell a hostile now holds is no give-up: the next one is taken.
    """
    reach = ground_by_hostile(w, policy)
    plan_avoid, plan_costly = _plan_blocked(w, m, policy, ctx, set().union(*reach.values()))
    params = grid_params(policy, plan_avoid, plan_costly)
    kept = committed_safe(m, w, HEAL_TARGET)
    target = reachable_safe_goal(m, w, known_safe_cells(w), params, town_cell(w, ctx.knowledge), reach, prefer=kept)
    if kept is not None and target != kept and safe_ruled_out(m, w, kept):
        _give_up_safe(m, w, kept)  # the one cell it committed to is out of reach: no second pick
        return None
    commit_safe(m, w, HEAL_TARGET, target)
    if target is None:
        return None
    if out := _walk_toward(w, m, policy, ctx, target, goal=goal):
        return out
    if nav_stuck.backed_off(m, goal, w.map_id, target, w.tick):
        _give_up_safe(m, w, target)
    return None  # else no step open this decision: the cell stays committed


def _give_up_safe(m: Memory, w: WorldModel, cell: Pos) -> None:
    """The walk to ``cell`` is over: rule it out for every safe walk and give
    safe ground up while that mark holds (``_safe_given_up``)."""
    m.safe_unreachable[(w.map_id, cell)] = w.tick
    commit_safe(m, w, HEAL_TARGET, None)
    m.heal_safe_given_up = (w.map_id, w.tick)


def _safe_given_up(m: Memory, w: WorldModel) -> bool:
    """Heal gave its safe tile up on this map, and health is not low or the
    give-up is younger than ``SAFE_UNREACHABLE_TICKS``. A full heal clears it
    (``note_heal_window``); low health lets one tile be tried again once the
    give-up lapses, so a reachable tile is never shut out for good."""
    g = m.heal_safe_given_up
    if g is None:
        return False
    if g[0] != w.map_id or (health_low(w) and w.tick - g[1] >= SAFE_UNREACHABLE_TICKS):
        m.heal_safe_given_up = None
        return False
    return True


def _walk_toward(
    w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, at: Pos, *, goal: str, avoid: set[Pos] = frozenset()
) -> StateOutcome | None:
    """One step along a cost path to ``at``, or None when there is no step there now.

    The walk is bounded like any other (``bounded_step``, A15): no path, or
    no progress in its window, gives ``at`` up with a backoff, so Heal tries
    the next food, a carried supply or a safe tile instead of pacing. A kept
    path that now crosses the reach of a known hostile it did not cross
    when planned is planned again.
    """
    plan_avoid, plan_costly = _plan_blocked(w, m, policy, ctx)
    plan_avoid = plan_avoid | avoid
    if m.goal == goal and hostiles_reaching(w, policy, m.path) - m.planned_threats:
        # A hostile that was not in reach of the path when it was planned is
        # now: plan again on the grid that prices its reach (A63 run 4).
        m.path = []

    def params():
        return grid_params(policy, plan_avoid, plan_costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, at, params(), nav=nav_search(m, w, goal, at))

    step = bounded_step(m, w, goal, at, plan_avoid, plan, params=params)
    # What the path crosses now it could not go round: only a hostile beyond it replans.
    m.planned_threats = hostiles_reaching(w, policy, m.path)
    return _out([set_position(step)], f"{goal} → {at}") if step is not None else None


def _act_food(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> StateOutcome | None:
    here = w.pos
    assert here is not None
    close = [f for f in food_in_sight(w, m) if chebyshev(f.pos, here) <= FOOD_REACH]
    # The food it went for first stays first while it is there (A71).
    c = targets_mod.committed(m, w, "heal_food")
    kept = [f for f in close if c is not None and f.id == c.target]
    if not kept:
        targets_mod.release(m, "heal_food")
    close = kept + [f for f in close if f not in kept]
    for food in close[:FOOD_CANDIDATES]:
        # A Take refused out of reach is sent again only from the food's own cell.
        reach = 0 if must_stand_on(m, food) else 1
        if chebyshev(food.pos, here) <= reach:
            nav_stuck.finish_in_reach(m, w, "heal_food")
            return _out([take(food.id)], f"take food {food.code}")
        if out := _cut_toward(w, m, policy, ctx, food.pos):
            return out
        # Walking onto it also picks up food eaten on pickup (golden cap).
        if out := _walk_toward(w, m, policy, ctx, food.pos, goal="heal_food"):
            targets_mod.commit(m, w, "heal_food", food.id)
            return out
    return None


def _cut_toward(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, at: Pos) -> StateOutcome | None:
    """Open the block between us and food at ``at``, Break's way, when nothing
    else leads there and what we hold opens it (a bush and a blade, A10).

    None when there is a walk to ``at`` or no block on the way we can open:
    the walk to it then decides, and gives it up if it goes nowhere. Food
    behind bushes the weapon cuts was given up ``no_path`` before (A16 Walk run 4).
    """
    choice = nominate_on_path(w, ctx.knowledge, w.pos, at)
    if choice is None:
        return None
    plan_avoid, plan_costly = _plan_blocked(w, m, policy, ctx)
    walk = cost_path(w, at, grid_params(policy, plan_avoid, plan_costly))
    if walk and walk[-1] == at:
        return None
    out = break_toward(w, ctx, choice, HealState.name)
    if not out.intents:
        return None
    out.reason = f"heal_food: {out.reason}"
    return out


def use_carried_heal(w: WorldModel, ctx: PlayContext) -> StateOutcome | None:
    """``Arm`` + ``Use`` self on carried food or a potion (API Use), one
    paced queue so both are sent. Retreat runs it too, when it is losing
    ground (A9). A potion goes only when ``healing.spend_potion`` says it is
    worth drinking now: health low, or the drink turns the fight (A96).

    The weapon to put back is remembered in ``heal_rearm`` and re-armed by
    ``_rearm_weapon`` on Heal's next decision, the drink done or not (A24). It
    is always a weapon (``equip.weapon_to_rearm``): the one armed now, else
    the last one armed this run, never the potion or tool in the slot (free-play
    run 4 re-armed a potion). A drink the server refused is not sent again
    into the same situation: the runner files the refusal's reason
    (``Runner._note_heal_refused``, ``healing.refusal_action``) and
    ``carried_heal`` skips that supply until it no longer applies. A ``Use``
    refused with nothing armed is sent next time with its ``Arm``. A drink
    that applied and drank nothing is held the same way, by its cause
    (``healing.noop_drink_cause``, A76).

    The ``Arm`` never goes out without its ``Use``: a cooldown that does not
    leave room for both in one queue sends nothing yet (``arm_then_use``).
    """
    m = ctx.memory
    item = carried_heal(w, m, ctx.policy, ctx.params)
    if item is None:
        return None
    # Another supply of the same code in the slot is not this one: arm it (A76).
    in_slot = w.armed_code == item.code and w.armed_id in (None, item.id)
    if in_slot and not must_arm(m, item.id):
        out = _out([use_self()], f"use {item.code}")
    elif queue := arm_and_use(w, m, item.id, use_self()):
        out = _out(queue, f"arm and use {item.code}")
        out.paced = True
    else:
        out = _out(None, f"wait out the cooldown to arm and use {item.code}")
        out.wait = True
        return out
    m.heal_drink = item.id
    if m.heal_rearm is None:
        m.heal_rearm = weapon_to_rearm(w, m)
    return out


def _rearm_weapon(w: WorldModel, m: Memory) -> StateOutcome:
    """Re-``Arm`` the weapon a drink swapped out (A24). Sends nothing when it is
    already armed or no longer in hand."""
    code = m.heal_rearm
    if intents := rearm_after_drink(w, m):
        return _out(intents, f"re-arm {code}")
    return _out(None, f"no re-arm needed for {code}")
