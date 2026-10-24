"""How Lorraine carries out an op (A141): each op maps to its walk and its intents.

The turn's op (the planner's answer at the goal, else the tree's own, A140)
is carried out here, one round at a time, on the ADK's executor and pathing:
every walk is ``pathing.guided_step`` (the A90 stuck ladder, give-ups and
backoff, A125's net progress) sent as a paced ``Step`` queue
(``executor.build_paced_walk_queue``), and a swing is paced by the armed
weapon's cooldown (``executor.build_attack_queue``, A153).

``OPS`` is the one table: an op's name to the function that works it. Each
returns an :class:`Act`: the intents to send this round (None sends nothing),
why, and whether the op is over (carried out, or nothing left it can do).
An op over is never run again for the same goal; the next walk of the tree
asks afresh. A new op goes in ``OPS``.

A walk keeps off the ground of every known hostile (``hostile_ground.walk_reach``,
A128) but the ones a fight with which fits her risk budget now
(``readiness.fits_now``): her one risk function, never a threshold of the walk's.

No op holds in silence (A168): a walk with no step gives its target up for
the ADK's backoff (``navigation.stuck``, A90) and is over, so the loop moves
on; an op with more than one target (gather, explore) takes the next-best
one that has a step first, and is over only once none has. Occupants on the
way are no reason to give a walk up (A169): only terrain is.

Every target has one value in gems, the ADK's (``agentrealm_adk.value``,
A170): gems, food (the health it would give back, at a potion's gems per
health), lives, and chests (what they hold that fits). Gather works the
target worth most per step (``gather_targets``), and every walk, whatever op
sent it, goes by a find on the way when its value beats the steps it adds
at the step price (``detour``). A step on clear ground is nearly free
(``value.STEP_GEMS``); hostile ground and other obstacles add the cost
grid's surcharges, and an attack or a threat her margin cannot pay for
(``urgent``) declines every detour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from agentrealm_adk import boss_clock
from agentrealm_adk.config import Policy
from agentrealm_adk.equip import ARMED, CUT, FIGHT, best_equip_upgrade, hunting_ceiling, weapon_to_arm
from agentrealm_adk.executor import (
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    arm_then_use,
    attack_cooldown_ticks,
    build_attack_queue,
    build_paced_walk_queue,
    trim_to_horizon,
    use_cooldown_ticks,
)
from agentrealm_adk.break_memory import choice_at
from agentrealm_adk.gem_caches import CacheTarget, cache_targets
from agentrealm_adk.gem_yield import GATHER_BLOCKS, region_of, region_yields
from agentrealm_adk.hostile_ground import GATHER_HOSTILE_RADIUS, hostiles_within, off_safe, walk_reach
from agentrealm_adk.knowledge_base import KnowledgeBase, knowledge_items
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation import cost_flood, walk_toward
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.passage import went_through
from agentrealm_adk.pathing import attempt_plan, grid_params, guided_step, plan_sets, route_ahead
from agentrealm_adk.reach import NEAR, supply_reach, weapon_reach
from agentrealm_adk.sellers import sellers_of, walk_order
from agentrealm_adk.survival import is_hostile, under_attack
from agentrealm_adk.targets import EXPLORE_ANYWHERE, GoalOp, explore_targets
from agentrealm_adk.travel.ops import travel_op_from_plan_goal
from agentrealm_adk.travel.resolve import resolve_travel
from agentrealm_adk.supplies import BLOCK_FOOD, kept_on_break
from agentrealm_adk.value import (
    URGENT,
    DropWatch,
    Find,
    allowance,
    bush_value,
    detour_gain,
    extra_steps,
    finds,
    net,
    step_gems,
)
from agentrealm_adk.world import DOORS, Entity, Pos, WorldModel, chebyshev

from .readiness import ORDINARY, Foes, fits_now, margin_now, owned, step_urgent

# Most cells one walk queue carries; the walk is re-planned once they have run.
WALK_CELLS = 8
# The goal a fetch_item's walk runs under: the given-up walks to a death chest
# the tree reads back (A167).
FETCH_GOAL = "lorraine:fetch_item"
# Most targets an op with many (gather, explore) tries in one round before it
# yields the round; the ones with no step are given up, so the next round
# tries the next ones.
TARGETS_TRIED = 6
GATHER, EXPLORE, DETOUR = "lorraine:gather", "lorraine:explore", "lorraine:detour"


@dataclass
class Started:
    """Where an op stood when it reached the top: what tells it done."""

    tick: int
    map_id: int | None
    owned: dict[str, int] = field(default_factory=dict)  # how many of each code she owned
    target: Pos | int | None = None  # what the op committed to: a frontier cell, an NPC id
    searched: set[tuple[int, Pos]] = field(default_factory=set)  # sellers reached without the item (buy)
    stood: set[Pos] = field(default_factory=set)  # frontier cells stood on (explore_area): seen from as near as can be
    detour: tuple[str, int] | None = None  # the find a detour walks to (A170): (kind, id)
    taken: set[tuple[str, int]] = field(default_factory=set)  # finds a take was sent for: never chosen again

    @classmethod
    def now(cls, w: WorldModel) -> Started:
        counts: dict[str, int] = {}
        for code in owned(w):
            counts[code] = counts.get(code, 0) + 1
        return cls(w.tick, w.map_id, counts)


@dataclass
class Ctx:
    """What carrying out an op reads: the world, the walk memory the ADK's
    pathing keeps, the knowledge base, the profile's policy, the world's
    clock, and the goal's value (what the risk budget prices by)."""

    world: WorldModel
    mem: Memory
    kb: KnowledgeBase | None
    policy: Policy
    started: Started
    tick_hz: int = DEFAULT_TICK_RATE_HZ
    horizon: int = QUEUE_HORIZON_INTENTS
    value: str = ORDINARY
    drops: DropWatch | None = None  # her bush cuts, judged for berries (A170)


@dataclass(frozen=True)
class Act:
    """One round of an op: the intents to send (None: nothing), why, whether
    it is over, and whether nothing is sent only on a wait the clock clears
    (``waits``: a weapon cooldown, her place not read yet, or occupants on
    her way, A169). Such a round is no stall and its repeat never ends the op (A168)."""

    intents: list[dict] | None
    why: str
    over: bool = False
    waits: bool = False
    # Over on a walk given up: the give-ups (``stuck.goal_key``) that must
    # stop holding, their ground changed, before it is walked again (A169, A171).
    given_up: tuple[str, ...] = ()


def act(op: GoalOp, ctx: Ctx) -> Act:
    """This round of ``op``: the function ``OPS`` names for it; over at once
    with why when it cannot be carried out as given (``refuse``)."""
    why = refuse(op)
    if why:
        return Act(None, why, over=True)
    return OPS[op["op"]](op, ctx)


def _int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def refuse(op: GoalOp | None) -> str:
    """Why ``op`` cannot be carried out as given, or "": not an op she runs,
    or a field it needs missing or of the wrong kind. The planner's reply is
    held to it before it runs (``ask.Planner.finish``)."""
    if not isinstance(op, dict) or op.get("op") not in OPS:
        return f"{op.get('op') if isinstance(op, dict) else op}: not an op she carries out"
    name = op["op"]
    cell = _int(op.get("x")) and _int(op.get("y"))
    if name in ("enter_level", "fight_boss") and not cell:
        return f"{name} needs the door's x and y"
    if name in ("fetch_item", "buy") and not (isinstance(op.get("code"), str) and op["code"]):
        return f"{name} needs a code"
    if name == "gather_gems" and not _int(op.get("count")):
        return "gather_gems needs a count"
    if name == "fight" and not (_int(op.get("npc_id")) or (isinstance(op.get("npc_type"), str) and op["npc_type"])):
        return "fight needs an npc_id or an npc_type"
    if name == "travel":
        if op.get("to") not in TRAVEL_TO:
            return f"travel to {op.get('to')!r}: not one of {', '.join(TRAVEL_TO)}"
        if op["to"] == "point" and not cell:
            return "travel to a point needs its x and y"
    return ""


# Where a ``travel`` may go; ``shop`` and ``entrance`` with no cell go to the nearest known one.
TRAVEL_TO = ("town", "point", "entrance", "shop", "hunting_ground")


# The intents she sends (API intents, https://agentrealm.gg/docs/api).


def take(supply_id: int) -> dict:
    return {"verb": "Take", "supply_id": supply_id}


def withdraw(chest_id: int) -> dict:
    """Everything that fits out of a ground chest on or next to her block."""
    return {"verb": "WithdrawFromChest", "chest_id": chest_id}


def arm(supply_id: int) -> dict:
    return {"verb": "Arm", "supply_id": supply_id}


def wear(supply_id: int) -> dict:
    return {"verb": "Wear", "supply_id": supply_id}


def remove(slot: str) -> dict:
    return {"verb": "Remove", "slot": slot}


def use_on(e: Entity) -> dict:
    """A swing at an NPC or a character: the server finds where it stands when the ``Use`` runs."""
    if e.kind == "npc":
        return {"verb": "Use", "target": {"kind": "npc", "npc_id": e.id}}
    return {"verb": "Use", "target": {"kind": "character", "character_id": e.id}}


def use_block(p: Pos) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": p[0], "y": p[1]}}


def use_self() -> dict:
    """A drink or a chest: ``Use`` on yourself, never a character target naming her own id."""
    return {"verb": "Use", "target": {"kind": "self"}}


# The walk every op takes.


def walk_sets(
    ctx: Ctx, toward: Entity | None = None, passing: tuple[Entity, ...] = ()
) -> tuple[set[Pos], set[Pos], frozenset[Pos]]:
    """``avoid``, ``costly`` and the hostile ground an op's walk keeps out
    of: every known hostile's (``walk_reach``) but those in view a fight with
    which fits her margin now (``readiness.fits_now``), the one the walk goes
    ``toward`` (a fight's), and the ones ``passing`` names, whose pass her
    margin covers together (a gather target beside them)."""
    w, m = ctx.world, ctx.mem
    _, avoid, costly = plan_sets(w, m, ctx.policy, ctx.kb)
    fit = {(e.kind, e.id) for e in w.entities if is_hostile(w, ctx.policy, e) and fits_now(e, w, ctx.kb, ctx.value)}
    fit |= {(e.kind, e.id) for e in (toward, *passing) if e is not None}
    reach = frozenset(off_safe(w, walk_reach(w, ctx.policy, skip=fit)))  # nothing hurts on a safe block (A162)
    return avoid, costly | reach, reach


def walk(
    ctx: Ctx, goal: str, dest: Pos, dest_map: int | None = None, toward: Entity | None = None,
    passing: tuple[Entity, ...] = (),
) -> Act:
    """One round of a walk to ``dest`` (on ``dest_map``, this map by
    default): the ADK's guided walk, sent as a paced queue of the path ahead.
    Occupants on the way are no obstacle (A169): the walk goes round them,
    else waits for them to move, a wait the clock clears (``Act.waits``) up
    to the ADK's cap (``stuck.OCCUPANT_WAIT_TICKS``, ``stuck.occupants_held``),
    after which the walk is over but its target is not given up: only
    terrain makes a target unreachable. With
    no step otherwise, it waits a round on a way whose first step is not
    open yet; else the target is given up for the ADK's backoff
    (``give_up_walk``) and the walk is over, so no op holds on it (A168).
    ``toward`` is the hostile a fight walks to and ``passing`` the hostiles
    a pass is covered for (``walk_sets``), whose ground it does not keep off.
    On the way, a find worth the steps it adds is taken first (``detour``)."""
    w, m = ctx.world, ctx.mem
    if w.pos is None or w.map_id is None:
        return Act(None, f"{goal}: unplaced", waits=True)
    dest_map = w.map_id if dest_map is None else dest_map
    side = detour(ctx, goal, dest if dest_map == w.map_id else None) if goal != DETOUR else None
    if side is not None:
        return side
    key = nav_stuck.goal_key(goal, dest_map, dest)
    if nav_stuck.is_backed_off(m.nav_stuck, key, w.tick):
        return given_up_act(ctx, goal, dest, key)
    avoid, costly, reach = walk_sets(ctx, toward, passing)

    def params():
        return grid_params(ctx.policy, avoid, costly, allow_goal_door=True, m=m, w=w, knowledge=ctx.kb)

    target: Pos | nav_stuck.Leg = dest
    if dest_map != w.map_id:
        path = walk_toward(w, ctx.kb, dest_map, dest, params(), stuck=m.nav_stuck)
        if not path:
            return Act(None, f"{goal}: no known way to map {dest_map}", over=True)
        target = nav_stuck.Leg(path[-1], key)
    step = guided_step(
        m, w, goal, target, avoid, attempt_plan(m, w, ctx.policy, avoid, costly, ctx.kb), ctx.kb, params=params, reach=reach
    )
    if step is None:
        if nav_stuck.is_backed_off(m.nav_stuck, key, w.tick):
            return given_up_act(ctx, goal, dest, key)
        if nav_stuck.occupants_held(m, w, goal, since=ctx.started.tick):
            # The ADK's cap on the wait (A169): over, its target not given up.
            return Act(None, f"{goal}: the way to {dest} stayed held by occupants for "
                             f"{nav_stuck.OCCUPANT_WAIT_TICKS} ticks", over=True)
        if nav_stuck.occupant_wait(m, w, goal) is not None:
            return Act(None, f"{goal}: the way to {dest} is held by occupants, waiting", waits=True)
        if m.goal == goal and m.path:
            return Act(None, f"{goal}: the way to {dest} is taken, waiting")
        give_up_walk(ctx, goal, key)
        return Act(None, f"{goal}: no step toward {dest}, given up", over=True, given_up=(key,))
    return Act(walk_queue(ctx, step), f"{goal} → {dest}")


# Value on the way (A170): one value for every target, one price for a step.


def urgent(ctx: Ctx) -> bool:
    """A step costs more than any find (``value.URGENT``): under attack,
    with a boss clock running, or with hostiles that can reach her that her
    margin does not cover (``readiness.step_urgent``). Hostile ground short
    of that is priced cell by cell by the cost grid (``walk_costs``)."""
    w = ctx.world
    return under_attack(w, ctx.policy) or boss_clock.running(ctx.mem, w) or step_urgent(w, ctx.kb, ctx.value)


def walk_costs(ctx: Ctx, cells: set[Pos]) -> dict[Pos, tuple[int, int]]:
    """The cost grid's price of the cheapest walk from her to each of
    ``cells`` she can reach, in steps, and how many steps that walk takes:
    the price is one a clear step plus every surcharge her walk keeps (a
    hostile's ground, a hazard, a break, fog; ``navigation.planner``), so
    price less steps is the surcharges alone (``surcharges``)."""
    w = ctx.world
    if w.pos is None or not cells:
        return {}
    avoid, costly, _ = walk_sets(ctx)
    params = grid_params(ctx.policy, avoid, costly, allow_goal_door=True, m=ctx.mem, w=w, knowledge=ctx.kb)
    return {p: (c, len(path)) for p, (path, c) in cost_flood(w, cells, params).items()}


def surcharges(cost: tuple[int, int]) -> int:
    """The surcharges in a walk's price (``walk_costs``): its price less its
    steps, never the steps round a wall, which the steps already count."""
    price, steps = cost
    return max(0, price - steps)


def take_find(ctx: Ctx, f: Find) -> list[dict]:
    """What takes a find: a ``Take`` of a supply, everything that fits out
    of a chest. Sent once an op: a find still there after it is not chosen
    again (``Started.taken``), so a take that came to nothing never loops."""
    ctx.started.taken.add((f.kind, f.id))
    return [take(f.id)] if f.kind == "supply" else [withdraw(f.id)]


def detour(ctx: Ctx, goal: str, dest: Pos | None) -> Act | None:
    """A side find on the walk ``goal`` is under way on (A170), any op's:
    the find whose value (``value.finds``) less the steps going by it adds
    (``value.extra_steps``, over the route ahead, and the surcharges on the
    walk to it, ``walk_costs``) at the step price (``value.step_gems``)
    gains most, when it gains at all; none while ``urgent``.
    Once taken up it is kept until it is taken or gone, the step price makes
    it a loss, or its walk ends; a take sent for it is never sent again.
    None: no detour, the walk goes on."""
    w, st = ctx.world, ctx.started
    if w.pos is None:
        return None
    step = step_gems(urgent(ctx))
    if step == URGENT:
        st.detour = None
        return None
    here = w.pos
    by_key = {(f.kind, f.id): f for f in finds(w, ctx.kb) if (f.kind, f.id) not in st.taken}
    f = by_key.get(st.detour) if st.detour else None
    if f is None:
        st.detour = None
        f = best_detour(ctx, goal, dest, step, list(by_key.values()))
        if f is None:
            return None
        st.detour = (f.kind, f.id)
    if chebyshev(here, f.pos) <= NEAR:
        st.detour = None
        return Act(take_find(ctx, f), f"detour: take {f.code} worth {f.gems:.1f} gems")
    a = walk(ctx, DETOUR, stand_by(w, f.pos, NEAR))
    if a.over:
        st.detour = None
        return None
    return Act(a.intents, f"detour → {f.code} worth {f.gems:.1f} gems at {f.pos}", waits=a.waits)


def best_detour(ctx: Ctx, goal: str, dest: Pos | None, step: float, candidates: list[Find]) -> Find | None:
    """The find on this map worth the most over the steps it adds to the
    route ahead of the walk ``goal`` and the surcharges on its way, when its
    value beats them; never the walk's own target, nor one on the ground of
    a hostile the walk keeps off or a walk to it given up."""
    w, m = ctx.world, ctx.mem
    here = w.pos
    route = route_ahead(w, m) if m.goal == goal else []
    if here is None or not route:
        return None
    _, _, reach = walk_sets(ctx)
    near = [
        f for f in candidates
        if f.map_id == w.map_id and f.pos not in reach
        and (dest is None or chebyshev(f.pos, dest) > NEAR)  # not the walk's own target
        and not nav_stuck.backed_off(m, DETOUR, w.map_id, stand_by(w, f.pos, NEAR), w.tick)
    ]
    costs = walk_costs(ctx, {stand_by(w, f.pos, NEAR) for f in near})
    best: tuple[float, int, Find] | None = None
    for f in near:
        extra = extra_steps(w, here, f.pos, route, allowance(f.gems, step), ctx.policy.avoid_blocks)
        cost = costs.get(stand_by(w, f.pos, NEAR))
        if extra is None or cost is None:
            continue
        # ``extra_steps`` counts the steps, walls and all; the walk adds only its surcharges.
        gain = detour_gain(f.gems, extra + surcharges(cost), step)
        if gain > 0 and (best is None or (gain, -f.id) > best[:2]):
            best = (gain, -f.id, f)
    return best[2] if best is not None else None


def given_up_act(ctx: Ctx, goal: str, dest: Pos, key: str) -> Act:
    """A walk to a target given up and still held: over, with the give-up
    that must lift first (A171: its ground must change)."""
    rec = ctx.mem.nav_stuck.given_up[key]
    return Act(None, f"{goal}: the walk to {dest} is {given_up_why(rec, ctx.world.tick)}", over=True, given_up=(key,))


def given_up_why(rec: nav_stuck.GiveUp, tick: int) -> str:
    """A give-up as the planner is told it (A171): why the walk was given up
    and what lifts it, never a bare "unreachable", so a ``cannot`` it draws
    names a reason that lapses with it."""
    return f"given up ({nav_stuck.why_given_up(rec)}); {nav_stuck.what_lifts(rec, tick)}"


# Her walks' goals: what she gave up on, which the tree and the planner never point back at (A169).
OWN_WALKS = "lorraine:"


def given_up_near(ctx: Ctx, map_id: int | None, p: Pos, reach: int = 0) -> nav_stuck.GiveUp | None:
    """A walk of hers given up on a cell within ``reach`` of ``p`` and still
    backed off (A90), the one that is backed off longest; None when none is.
    A walk to a seller stands next to it, so its give-up is near, not on, it."""
    tick = ctx.world.tick
    near = [
        g for g in ctx.mem.nav_stuck.given_up.values()
        if g.goal.startswith(OWN_WALKS) and g.map_id == map_id and chebyshev(g.target, p) <= reach and g.holds(tick)
    ]
    return max(near, key=lambda g: g.until) if near else None


def given_up_lines(m: Memory, tick: int) -> list[str]:
    """Each walk target of hers given up and still held, for the planner
    (A169): where, why the walk was given up, and what lifts it (A171)."""
    recs = sorted(
        (g for g in m.nav_stuck.given_up.values() if g.goal.startswith(OWN_WALKS) and g.holds(tick)),
        key=lambda g: (g.until, g.map_id, g.target),
    )
    return [
        f"{g.goal.removeprefix(OWN_WALKS)} to map {g.map_id} at {g.target[0]},{g.target[1]}: walk {given_up_why(g, tick)}"
        for g in recs
    ]


def given_up_target(op: GoalOp, ctx: Ctx) -> str:
    """Why ``op`` points back at a target her walk gave up and is still backed
    off from (A169), or "": its cell, or for a ``buy`` every seller of its item."""
    w = ctx.world
    if op.get("op") == "buy":
        sellers = [s for s in sellers_of(op.get("code", ""), w, ctx.kb, op) if s.in_stock]
        ups = [given_up_near(ctx, s.map_id, s.pos, NEAR) for s in sellers]
        if sellers and all(ups):
            return f"every seller of {op.get('code')} is {given_up_why(min(ups, key=lambda g: g.until), w.tick)}"
        return ""
    if _int(op.get("x")) and _int(op.get("y")):
        map_id = op["map_id"] if _int(op.get("map_id")) else w.map_id
        g = given_up_near(ctx, map_id, (op["x"], op["y"]), NEAR)
        if g is not None:
            return f"the walk to {op['x']},{op['y']} is {given_up_why(g, w.tick)}"
    return ""


def give_up_walk(ctx: Ctx, goal: str, key: str) -> None:
    """Give up the walk target ``key`` names (``navigation.stuck.goal_key``)
    for the ADK's backoff and until the ground round it changes (A90, A171):
    through its attempt when one is tracked, so its path is dropped and the
    ``stuck`` signal raised, else the record alone."""
    m, w = ctx.mem, ctx.world
    stuck = m.nav_stuck
    if nav_stuck.is_backed_off(stuck, key, w.tick):
        return
    att = next((a for a in stuck.attempts.values() if (a.backoff_key or a.key) == key), None)
    if att is not None:
        nav_stuck.give_up(m, w, att, "no_step")
    else:
        map_id, target = nav_stuck.key_dest(key)
        ground = nav_stuck.ground_seen(w, target) if map_id == w.map_id else None
        nav_stuck.remember_give_up(stuck, key, goal, w.tick, ground).reasons = ("no_step",)


def walk_queue(ctx: Ctx, step: Pos) -> list[dict]:
    """``step`` and the cells after it on the walk's path, one block each,
    up to ``WALK_CELLS`` and never past a door (it moves her), paced by her
    movement speed from her last step. The cells queued are cut off the
    path, so the walk goes on from where the queue ends."""
    w, m = ctx.world, ctx.mem
    path = m.path[m.path.index(step):] if step in m.path else [step]
    cells: list[Pos] = []
    prev = w.pos
    for p in path[:WALK_CELLS]:
        if prev is None or chebyshev(prev, p) != 1:
            break
        cells.append(p)
        prev = p
        if w.view.tiles.get(p) in DOORS:
            break
    if step in m.path:
        m.path = path[len(cells):]
    since = max(1, w.tick - m.last_step_tick) if m.last_step_tick is not None else None
    queue = build_paced_walk_queue(
        w.pos, cells, movement_speed_milli=w.movement_speed, tick_rate_hz=ctx.tick_hz, ticks_since_last_step=since
    )
    return trim_to_horizon(queue, limit=ctx.horizon)


def stand_by(w: WorldModel, p: Pos, reach: int) -> Pos:
    """Where to walk to act on ``p`` within ``reach``: ``p`` itself when it
    can be stood on, else the free cell within ``reach`` of it nearest her
    (a shelf, a seller, A169: a person is reached by interaction range, never
    by standing on their cell). With every such cell held, the one nearest
    her: the walk goes up to it and waits for its occupant (``walk``)."""
    held = w.occupied() | w.for_sale()
    if reach == 0 or (w.walkable(p) and p not in held):
        return p
    if w.pos is None:
        return p
    near = [
        (p[0] + dx, p[1] + dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1) if dx or dy
    ]
    stand = [n for n in near if w.walkable(n) and n not in w.for_sale()]
    free = [n for n in stand if n not in held] or stand
    return min(free, key=lambda n: (chebyshev(n, w.pos), n)) if free else p


def reach_and(
    ctx: Ctx, goal: str, p: Pos, reach: int, then: Callable[[], list[dict]], why: str, toward: Entity | None = None,
    passing: tuple[Entity, ...] = (),
) -> Act:
    """``then``'s intents once ``p`` is within ``reach``, else a walk there."""
    w = ctx.world
    if w.pos is not None and chebyshev(w.pos, p) <= reach:
        intents = then()
        return Act(intents or None, why if intents else f"{why}: waiting on a cooldown", waits=not intents)
    return walk(ctx, goal, stand_by(w, p, reach), toward=toward, passing=passing)


def gained(ctx: Ctx, code: str) -> bool:
    """She owns more of ``code`` than when the op started (held, stowed, armed or worn)."""
    return sum(1 for c in owned(ctx.world) if c == code) > ctx.started.owned.get(code, 0)


def lying(w: WorldModel, code: str, *, priced: bool) -> list[Entity]:
    """The supplies of ``code`` in sight, for sale (``priced``) or free, nearest first."""
    here = w.pos or (0, 0)
    found = [e for e in w.entities if e.kind == "supply" and e.code == code and (e.gem_price is not None) == priced]
    return sorted(found, key=lambda e: (chebyshev(here, e.pos), e.id))


# The ops.


def travel(op: GoalOp, ctx: Ctx) -> Act:
    """Walk to where the travel names (``travel.resolve``); over once there."""
    w = ctx.world
    # No cell means the nearest known one, as 0, 0 does (``travel_op_from_plan_goal``).
    where = {"x": 0, "y": 0, **op} if not (_int(op.get("x")) and _int(op.get("y"))) else op
    dest = resolve_travel(travel_op_from_plan_goal(where), w, ctx.kb, ctx.mem.strength)
    if dest is None:
        return Act(None, f"travel to {op.get('to')}: nowhere known", over=True)
    if (w.map_id, w.pos) == (dest.map_id, dest.pos) or went_through(op, w, ctx.started.tick, ctx.started.map_id):
        return Act(None, f"at {dest.label} {dest.pos}", over=True)
    return walk(ctx, f"lorraine:travel:{dest.label}", dest.pos, dest.map_id)


def enter_level(op: GoalOp, ctx: Ctx) -> Act:
    """Walk onto the level's door; over once through it, or standing on a
    door that did not take her (locked: the next walk asks again)."""
    w = ctx.world
    door = (op["x"], op["y"])
    if went_through(op, w, ctx.started.tick, ctx.started.map_id) or w.map_id != ctx.started.map_id:
        return Act(None, f"through the door at {door}", over=True)
    if w.pos == door:
        return Act(None, f"the door at {door} did not take her", over=True)
    return walk(ctx, "lorraine:enter_level", door)


def fetch_item(op: GoalOp, ctx: Ctx) -> Act:
    """Walk to the item and ``Take`` it; over once it is hers, or gone from
    sight. With a ``chest_id``, from that chest (A167): walk next to it and
    withdraw all that fits; over once the item is hers, or the chest is in
    reach without it."""
    w, code = ctx.world, op.get("code", "")
    if gained(ctx, code):
        return Act(None, f"took {code}", over=True)
    if _int(op.get("chest_id")):
        return from_chest(op, ctx)
    cell = (op["x"], op["y"]) if isinstance(op.get("x"), int) and isinstance(op.get("y"), int) else None
    found = [e for e in lying(w, code, priced=False) if cell is None or e.pos == cell] or lying(w, code, priced=False)
    if not found:
        if cell is not None and w.pos is not None and chebyshev(w.pos, cell) > w.perception:
            return walk(ctx, FETCH_GOAL, cell)
        return Act(None, f"no {code} in sight", over=True)
    item = found[0]
    return reach_and(ctx, FETCH_GOAL, item.pos, NEAR, lambda: [take(item.id)], f"take {code}")


def from_chest(op: GoalOp, ctx: Ctx) -> Act:
    """``fetch_item``'s walk to a chest and its withdraw (A167). A withdraw
    that leaves the item in the chest (the pack full, or the withdraw
    refused) backs off a walk to the chest, as a given-up walk is, so the
    tree passes it over (``goals.walk``'s ``unreachable``) and buys. A chest
    seen without the item, or gone from its cell, needs no mark: the world's
    look replaces what it holds, or drops it."""
    w, code, chest_id = ctx.world, op.get("code", ""), op["chest_id"]
    contents = w.chest_contents.get(chest_id)
    if contents is not None:
        if not any(s.code == code for s in contents):
            return Act(None, f"chest {chest_id} holds no {code}", over=True)
        if ctx.started.target == chest_id:
            chest = w.death_chests.get(chest_id)
            if chest is not None and ctx.mem is not None:
                give_up_walk(ctx, FETCH_GOAL, nav_stuck.goal_key(FETCH_GOAL, chest.map_id, chest.pos))
            return Act(None, f"withdrew from chest {chest_id}; {code} did not fit", over=True)
        ctx.started.target = chest_id
        return Act([withdraw(chest_id)], f"take {code} back from chest {chest_id}")
    cell = (op["x"], op["y"]) if _int(op.get("x")) and _int(op.get("y")) else None
    if cell is None or (w.pos is not None and chebyshev(w.pos, cell) <= NEAR):
        return Act(None, f"chest {chest_id} is not here", over=True)
    return walk(ctx, FETCH_GOAL, stand_by(w, cell, NEAR))


def buy(op: GoalOp, ctx: Ctx) -> Act:
    """Take the item off a shelf in sight, else walk to the nearest seller
    known for it, not sold out (``sellers.sellers_of``, A131, A159) and not
    given up by her walk (``given_up_near``, A169); over once it is hers or
    no seller is left to try."""
    w, code = ctx.world, op.get("code", "")
    if gained(ctx, code):
        return Act(None, f"bought {code}", over=True)
    shelf = lying(w, code, priced=True)
    if shelf:
        item = shelf[0]
        return reach_and(ctx, "lorraine:buy", item.pos, NEAR, lambda: [take(item.id)], f"buy {code} for {item.gem_price}")
    sellers = sellers_of(code, w, ctx.kb, op)
    if w.pos is not None:
        # A seller reached with no shelf of it in sight does not sell it now.
        ctx.started.searched |= {(s.map_id, s.pos) for s in sellers if s.map_id == w.map_id and chebyshev(w.pos, s.pos) <= NEAR}
    left = [s for s in sellers if s.in_stock and (s.map_id, s.pos) not in ctx.started.searched]
    # A seller her walk gave up on is not walked to again until its give-up lifts (A90, A169, A171).
    ups = [g for s in left if (g := given_up_near(ctx, s.map_id, s.pos, NEAR)) is not None]
    walkable = [s for s in left if given_up_near(ctx, s.map_id, s.pos, NEAR) is None]
    if not walkable and ups:
        first = min(ups, key=lambda g: g.until)
        return Act(None, f"every seller of {code} left is {given_up_why(first, w.tick)}", over=True,
                   given_up=tuple(sorted({nav_stuck.key_of(g) for g in ups})))
    left = walkable
    if not left:
        return Act(None, f"no seller of {code} left to try", over=True)
    seller = min(left, key=lambda s: walk_order(s, w))
    dest = stand_by(w, seller.pos, NEAR) if seller.map_id == w.map_id else seller.pos
    return walk(ctx, "lorraine:buy", dest, seller.map_id)


def equip(op: GoalOp, ctx: Ctx) -> Act:
    """Arm and wear the best gear held (``equip.best_equip_upgrade``, A99); over when nothing beats what is on."""
    w, kb = ctx.world, ctx.kb
    up = best_equip_upgrade(w, knowledge_items(kb), w.threat, ctx.mem, ceiling=hunting_ceiling(w, kb))
    if up is None:
        return Act(None, "best gear on", over=True)
    if up.slot == ARMED:
        return Act([arm(up.supply_id)], f"arm {up.code}")
    return Act([remove(up.slot), wear(up.supply_id)] if up.remove_first else [wear(up.supply_id)], f"wear {up.code}")


@dataclass(frozen=True)
class Target:
    """One place an op with many may work: the cell, the reach it is worked
    from, what is sent there, why, and the hostiles beside it whose pass her
    margin covers (``gather_targets``)."""

    cell: Pos
    reach: int
    then: Callable[[], list[dict]]
    why: str
    foes: tuple[Entity, ...] = ()
    gems: float = 0.0  # what working it is worth, in gems (``value``, A170): ranked by its net


def gather_gems(op: GoalOp, ctx: Ctx) -> Act:
    """Until ``count`` gems are held: work the best gather target that has a
    step (``gather_targets``), else explore the region the op names; over
    once neither has (A168)."""
    w = ctx.world
    count = op.get("count", 0)
    if (w.gems or 0) >= count:
        return Act(None, f"{w.gems} gems held", over=True)
    a = first_with_a_step(ctx, GATHER, gather_targets(ctx))
    if a is not None:
        return a
    a = explore_area({k: op[k] for k in ("x", "y", "radius") if k in op}, ctx)
    return a if not a.over else Act(None, f"no gather target has a step ({a.why})", over=True)


def gather_targets(ctx: Ctx) -> list[Target]:
    """Where a gather may work, best first (A168, A170, A172): every find in
    sight or remembered on this map (``value.finds``: gems, food while it
    would heal, lives, chests), an oddity a held capability may break
    (``gem_caches.cache_targets``, worth its expected cache), grass to cut
    (worth the gems a cut drops there, ``gem_yield.region_yields``) and
    bushes a held item cuts (``value.bush_value``, none left standing by a
    cut), each by its net (``value.net``: its value less the gems of the walk
    to it, surcharges and all, ``walk_costs``), distance breaking ties, none
    given up and still backed off. A target with known hostiles beside it
    (``hostile_ground.hostiles_within`` ``GATHER_HOSTILE_RADIUS``) comes after
    every other, and only when her margin now covers them all
    (``readiness.margin_now``, A139); its walk then passes their ground."""
    w, m, kb = ctx.world, ctx.mem, ctx.kb
    if w.pos is None:
        return []
    here = w.pos
    found: list[Target] = []
    for f in finds(w, kb):
        if (f.kind, f.id) not in ctx.started.taken:
            found.append(Target(f.pos, NEAR, lambda f=f: take_find(ctx, f), f"take {f.code} worth {f.gems:.1f} gems",
                                gems=f.gems))
    for c in cache_targets(w, kb, ctx.tick_hz):
        found.append(Target(c.pos, supply_reach(kb, c.supply.code), lambda c=c: break_oddity(ctx, c),
                            f"break the {c.block} at {c.pos} for its cache, about {c.gems:.1f} gems", gems=c.gems))
    reach = weapon_reach(w, kb)
    rate, by_region = region_yields(kb, w.map_id)
    for p, b in w.view.tiles.items():
        if b in GATHER_BLOCKS:
            found.append(Target(p, reach, lambda p=p: cut(ctx, p), f"cut {p}", gems=by_region.get(region_of(p), rate)))
        elif b in BLOCK_FOOD and (gems := bush_value(b, w, kb)) > 0 and cuts_bush(ctx, p):
            found.append(Target(p, NEAR, lambda p=p: cut(ctx, p), f"cut the bush at {p} for food worth {gems:.1f} gems",
                                gems=gems))
    kept = [(t, stand) for t in found if not nav_stuck.backed_off(m, GATHER, w.map_id, stand := stand_by(w, t.cell, t.reach), w.tick)]
    costs = walk_costs(ctx, {stand for _, stand in kept})
    out: list[tuple[tuple, Target]] = []
    for t, stand in kept:
        foes = tuple(hostiles_within(w, ctx.policy, t.cell, GATHER_HOSTILE_RADIUS))
        if foes and not margin_now(Foes(foes, ctx.value), w, kb).ok:
            continue
        if foes:
            t = Target(t.cell, t.reach, t.then, f"{t.why}, past {len(foes)} hostile(s)", foes, t.gems)
        cost = costs[stand][0] if stand in costs else chebyshev(here, stand)
        out.append(((bool(foes), -net(t.gems, cost), chebyshev(here, t.cell), t.cell), t))
    return [t for _, t in sorted(out, key=lambda x: x[0])]


def cuts_bush(ctx: Ctx, p: Pos) -> bool:
    """A held item opens the bush at ``p`` without being used up
    (``break_memory.choice_at``, ``supplies.kept_on_break``: a knife or a
    sword, never a match), and no cut of hers left it standing lately."""
    w = ctx.world
    if ctx.drops is not None and ctx.drops.unbroken(w, p):
        return False
    choice = choice_at(w, ctx.kb, p)
    return choice is not None and kept_on_break(choice.supply.code)


def first_with_a_step(ctx: Ctx, goal: str, targets: list[Target]) -> Act | None:
    """The first of ``targets`` (best first) that sends something: one within
    reach is worked, else walked to. Each with no step is given up by its
    walk (``walk``) and the next tried, up to ``TARGETS_TRIED`` a round. None
    when none of them is left; a round with no step among those tried says so,
    and the next round tries the ones after."""
    for tried, t in enumerate(targets):
        if tried >= TARGETS_TRIED:
            return Act(None, f"{goal}: no step toward {targets[0].cell} nor the next {tried - 1} targets, given up")
        a = reach_and(ctx, goal, t.cell, t.reach, t.then, t.why, passing=t.foes)
        if not a.over:
            return a
    return None


def cut(ctx: Ctx, cell: Pos) -> list[dict]:
    """A ``Use`` on ``cell`` with the cutter in hand, armed first when it is
    not, behind the cooldown its swing still owes (A153)."""
    w, m = ctx.world, ctx.mem
    cutter = weapon_to_arm(w, ctx.kb, CUT, hunting_ceiling(w, ctx.kb))
    use = use_block(cell)
    since = max(1, w.tick - m.last_use_tick) if m.last_use_tick is not None else None
    if cutter is None or cutter.code == w.armed_code:
        return build_attack_queue(
            [use], poll_interval_ticks=ctx.horizon, horizon_ticks=ctx.horizon,
            weapon_cooldown_ticks=attack_cooldown_ticks(w.armed_code, ctx.tick_hz), ticks_since_last_use=since,
        )
    return arm_then_use(
        arm(cutter.id), use, cooldown_ticks=use_cooldown_ticks(use, cutter.code, ctx.tick_hz),
        horizon_ticks=ctx.horizon, ticks_since_last_use=since,
    )


def break_oddity(ctx: Ctx, c: CacheTarget) -> list[dict]:
    """A ``Use`` on the oddity with the supply that breaks it in hand, armed
    first when it is not, behind the cooldown a swing still owes (A153);
    the break is filed in flight (``Memory.break_pending``), so the round
    trip settles it into break memory (``break_results``)."""
    w, m = ctx.world, ctx.mem
    use = use_block(c.pos)
    since = max(1, w.tick - m.last_use_tick) if m.last_use_tick is not None else None
    if c.supply.id < 0 or c.supply.code == w.armed_code:
        # A tool's Use (matches, a bomb) waits on no swing's cooldown (A153).
        intents = build_attack_queue(
            [use], poll_interval_ticks=ctx.horizon, horizon_ticks=ctx.horizon,
            weapon_cooldown_ticks=use_cooldown_ticks(use, w.armed_code, ctx.tick_hz), ticks_since_last_use=since,
        )
    else:
        intents = arm_then_use(
            arm(c.supply.id), use, cooldown_ticks=use_cooldown_ticks(use, c.supply.code, ctx.tick_hz),
            horizon_ticks=ctx.horizon, ticks_since_last_use=since,
        )
    if intents and w.map_id is not None:
        m.break_pending = (w.map_id, c.pos, c.capability)
    return intents


def swings(ctx: Ctx, foe: Entity, count: int = 1) -> list[dict]:
    """``count`` swings at ``foe``, each behind the cooldown the weapon in
    hand still owes (A153). With another weapon to fight with
    (``equip.weapon_to_arm``, A99) than the one in hand, a cutter or a heal
    say, it is armed first and one swing follows it."""
    w, m = ctx.world, ctx.mem
    since = max(1, w.tick - m.last_use_tick) if m.last_use_tick is not None else None
    weapon = weapon_to_arm(w, ctx.kb, FIGHT, hunting_ceiling(w, ctx.kb))
    use = use_on(foe)
    if weapon is not None and weapon.code != w.armed_code:
        return arm_then_use(
            arm(weapon.id), use, cooldown_ticks=use_cooldown_ticks(use, weapon.code, ctx.tick_hz),
            horizon_ticks=ctx.horizon, ticks_since_last_use=since,
        )
    return build_attack_queue(
        [use] * count, poll_interval_ticks=ctx.horizon, horizon_ticks=ctx.horizon,
        weapon_cooldown_ticks=attack_cooldown_ticks(w.armed_code, ctx.tick_hz), ticks_since_last_use=since,
    )


def explore_area(op: GoalOp, ctx: Ctx) -> Act:
    """Walk to the nearest frontier cell in the area (``targets.explore_targets``)
    not given up nor stood on already, kept until reached; one with no step
    is given up and the next nearest taken (A168); over when none is left."""
    w, m, st = ctx.world, ctx.mem, ctx.started
    if w.pos is None:
        return Act(None, "explore: unplaced", waits=True)
    st.stood.add(w.pos)
    area = {"x": w.pos[0], "y": w.pos[1], "radius": EXPLORE_ANYWHERE, **op}
    cells = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, explore_targets(area, w), w.tick) - st.stood
    cells = {p for p in cells if not nav_stuck.backed_off(m, EXPLORE, w.map_id, p, w.tick)}
    if st.target not in cells:
        st.target = None
    order = sorted(cells, key=lambda p: (p != st.target, chebyshev(w.pos, p), p))
    for tried, p in enumerate(order):
        if tried >= TARGETS_TRIED:
            return Act(None, f"{EXPLORE}: no step toward {order[0]} nor the next {tried - 1} frontier cells, given up")
        a = walk(ctx, EXPLORE, p)
        if not a.over:
            st.target = p
            return a
    st.target = None
    return Act(None, "nothing left to explore there", over=True)


def fight(op: GoalOp, ctx: Ctx) -> Act:
    """Close on the op's NPC (``npc_id``, else the nearest of ``npc_type``,
    else the boss in sight for ``fight_boss``) and swing until it is gone;
    a ``fight_boss`` with no boss in sight walks to its door."""
    w, st = ctx.world, ctx.started
    if w.pos is None:
        return Act(None, "fight: unplaced", waits=True)
    foe = next((e for e in w.entities if e.kind == "npc" and e.id == st.target), None)
    if foe is None and st.target is not None:
        return Act(None, f"npc {st.target} gone", over=True)
    if foe is None:
        foe = pick_foe(op, w)
    if foe is None:
        if op["op"] == "fight_boss" and isinstance(op.get("x"), int) and isinstance(op.get("y"), int):
            return walk(ctx, "lorraine:fight_boss", (op["x"], op["y"]))
        return Act(None, "no such hostile in sight", over=True)
    st.target = foe.id
    reach = weapon_reach(w, ctx.kb)

    return reach_and(ctx, f"lorraine:{op['op']}", foe.pos, reach, lambda: swings(ctx, foe, 3), f"swing at npc {foe.id}", toward=foe)


def pick_foe(op: GoalOp, w: WorldModel) -> Entity | None:
    here = w.pos or (0, 0)
    npcs = [e for e in w.entities if e.kind == "npc"]
    if isinstance(op.get("npc_id"), int):
        npcs = [e for e in npcs if e.id == op["npc_id"]]
    elif op.get("npc_type"):
        npcs = [e for e in npcs if e.code == op["npc_type"]]
    elif op["op"] == "fight_boss":
        npcs = [e for e in npcs if e.is_boss]
    else:
        return None
    return min(npcs, key=lambda e: (chebyshev(here, e.pos), e.id)) if npcs else None


# The one table: an op's name to what works it.
OPS: dict[str, Callable[[GoalOp, Ctx], Act]] = {
    "travel": travel,
    "enter_level": enter_level,
    "fetch_item": fetch_item,
    "buy": buy,
    "equip": equip,
    "gather_gems": gather_gems,
    "explore_area": explore_area,
    "fight": fight,
    "fight_boss": fight,
}
