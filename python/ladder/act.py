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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

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
from agentrealm_adk.gem_yield import GATHER_BLOCKS, take_raises_gems
from agentrealm_adk.hostile_ground import off_safe, walk_reach
from agentrealm_adk.knowledge_base import KnowledgeBase, knowledge_items
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation import walk_toward
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.passage import went_through
from agentrealm_adk.pathing import attempt_plan, grid_params, guided_step, plan_sets
from agentrealm_adk.reach import NEAR, weapon_reach
from agentrealm_adk.sellers import sellers_of, walk_order
from agentrealm_adk.survival import is_hostile
from agentrealm_adk.targets import EXPLORE_ANYWHERE, GoalOp, explore_targets
from agentrealm_adk.travel.ops import travel_op_from_plan_goal
from agentrealm_adk.travel.resolve import resolve_travel
from agentrealm_adk.world import DOORS, Entity, Pos, WorldModel, chebyshev

from .readiness import ORDINARY, fits_now, owned

# Most cells one walk queue carries; the walk is re-planned once they have run.
WALK_CELLS = 8


@dataclass
class Started:
    """Where an op stood when it reached the top: what tells it done."""

    tick: int
    map_id: int | None
    owned: dict[str, int] = field(default_factory=dict)  # how many of each code she owned
    target: Pos | int | None = None  # what the op committed to: a frontier cell, an NPC id
    searched: set[tuple[int, Pos]] = field(default_factory=set)  # sellers reached without the item (buy)
    stood: set[Pos] = field(default_factory=set)  # frontier cells stood on (explore_area): seen from as near as can be

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


@dataclass(frozen=True)
class Act:
    """One round of an op: the intents to send (None: nothing), why, and whether it is over."""

    intents: list[dict] | None
    why: str
    over: bool = False


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


def walk_sets(ctx: Ctx, toward: Entity | None = None) -> tuple[set[Pos], set[Pos], frozenset[Pos]]:
    """``avoid``, ``costly`` and the hostile ground an op's walk keeps out
    of: every known hostile's (``walk_reach``) but those in view a fight with
    which fits her margin now (``readiness.fits_now``), and the one the walk
    goes ``toward`` (a fight's)."""
    w, m = ctx.world, ctx.mem
    _, avoid, costly = plan_sets(w, m, ctx.policy, ctx.kb)
    fit = {(e.kind, e.id) for e in w.entities if is_hostile(w, ctx.policy, e) and fits_now(e, w, ctx.kb, ctx.value)}
    if toward is not None:
        fit.add((toward.kind, toward.id))
    reach = frozenset(off_safe(w, walk_reach(w, ctx.policy, skip=fit)))  # nothing hurts on a safe block (A162)
    return avoid, costly | reach, reach


def walk(ctx: Ctx, goal: str, dest: Pos, dest_map: int | None = None, toward: Entity | None = None) -> Act:
    """One round of a walk to ``dest`` (on ``dest_map``, this map by
    default): the ADK's guided walk, sent as a paced queue of the path ahead.
    Nothing to send when no step is found (the stuck ladder takes it from
    there); over once the walk is given up and backed off. ``toward`` is the
    hostile a fight walks to, whose ground it does not keep off."""
    w, m = ctx.world, ctx.mem
    if w.pos is None or w.map_id is None:
        return Act(None, f"{goal}: unplaced")
    dest_map = w.map_id if dest_map is None else dest_map
    key = nav_stuck.goal_key(goal, dest_map, dest)
    if nav_stuck.is_backed_off(m.nav_stuck, key, w.tick):
        return Act(None, f"{goal}: the walk to {dest} was given up", over=True)
    avoid, costly, reach = walk_sets(ctx, toward)

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
        return Act(None, f"{goal}: no step toward {dest}")
    return Act(walk_queue(ctx, step), f"{goal} → {dest}")


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
    can be stood on, else its nearest open neighbour (a shelf, an NPC)."""
    if reach == 0 or (w.walkable(p) and p not in w.occupied() and p not in w.for_sale()):
        return p
    near = w.open_neighbours(p)
    return min(near, key=lambda n: (chebyshev(n, w.pos), n)) if near and w.pos is not None else p


def reach_and(
    ctx: Ctx, goal: str, p: Pos, reach: int, then: Callable[[], list[dict]], why: str, toward: Entity | None = None
) -> Act:
    """``then``'s intents once ``p`` is within ``reach``, else a walk there."""
    w = ctx.world
    if w.pos is not None and chebyshev(w.pos, p) <= reach:
        intents = then()
        return Act(intents or None, why if intents else f"{why}: waiting on a cooldown")
    return walk(ctx, goal, stand_by(w, p, reach), toward=toward)


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
            return walk(ctx, "lorraine:fetch_item", cell)
        return Act(None, f"no {code} in sight", over=True)
    item = found[0]
    return reach_and(ctx, "lorraine:fetch_item", item.pos, NEAR, lambda: [take(item.id)], f"take {code}")


def from_chest(op: GoalOp, ctx: Ctx) -> Act:
    """``fetch_item``'s walk to a chest and its withdraw (A167)."""
    w, code, chest_id = ctx.world, op.get("code", ""), op["chest_id"]
    contents = w.chest_contents.get(chest_id)
    if contents is not None:
        if not any(s.code == code for s in contents):
            return Act(None, f"chest {chest_id} holds no {code}", over=True)
        if ctx.started.target == chest_id:
            return Act(None, f"withdrew from chest {chest_id}; {code} did not fit", over=True)
        ctx.started.target = chest_id
        return Act([withdraw(chest_id)], f"take {code} back from chest {chest_id}")
    cell = (op["x"], op["y"]) if _int(op.get("x")) and _int(op.get("y")) else None
    if cell is None or (w.pos is not None and chebyshev(w.pos, cell) <= NEAR):
        return Act(None, f"chest {chest_id} is not here", over=True)
    return walk(ctx, "lorraine:fetch_item", stand_by(w, cell, NEAR))


def buy(op: GoalOp, ctx: Ctx) -> Act:
    """Take the item off a shelf in sight, else walk to the nearest seller
    known for it and not sold out (``sellers.sellers_of``, A131, A159); over
    once it is hers or no seller is left to try."""
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


def gather_gems(op: GoalOp, ctx: Ctx) -> Act:
    """Until ``count`` gems are held: take a gem in sight, else cut the
    nearest grass with the cutter ``equip.weapon_to_arm`` picks, else explore
    the region the op names."""
    w, m = ctx.world, ctx.mem
    count = op.get("count", 0)
    if (w.gems or 0) >= count:
        return Act(None, f"{w.gems} gems held", over=True)
    gems = [e for e in w.entities if e.kind == "supply" and e.gem_price is None and take_raises_gems(e.code)]
    if gems and w.pos is not None:
        gem = min(gems, key=lambda e: (chebyshev(w.pos, e.pos), e.id))
        return reach_and(ctx, "lorraine:gather", gem.pos, NEAR, lambda: [take(gem.id)], "take a gem")
    grass = sorted(p for p, b in w.view.tiles.items() if b in GATHER_BLOCKS)
    grass = [p for p in grass if not nav_stuck.backed_off(m, "lorraine:gather", w.map_id, p, w.tick)]
    if grass and w.pos is not None:
        cell = min(grass, key=lambda p: (chebyshev(w.pos, p), p))
        return reach_and(ctx, "lorraine:gather", cell, weapon_reach(w, ctx.kb), lambda: cut(ctx, cell), f"cut {cell}")
    return explore_area({k: op[k] for k in ("x", "y", "radius") if k in op}, ctx)


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
    not given up nor stood on already, kept until reached; over when none is left."""
    w, m, st = ctx.world, ctx.mem, ctx.started
    if w.pos is None:
        return Act(None, "explore: unplaced")
    st.stood.add(w.pos)
    area = {"x": w.pos[0], "y": w.pos[1], "radius": EXPLORE_ANYWHERE, **op}
    cells = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, explore_targets(area, w), w.tick) - st.stood
    if st.target not in cells:
        st.target = min(cells, key=lambda p: (chebyshev(w.pos, p), p)) if cells else None
    if st.target is None:
        return Act(None, "nothing left to explore there", over=True)
    return walk(ctx, "lorraine:explore", st.target)


def fight(op: GoalOp, ctx: Ctx) -> Act:
    """Close on the op's NPC (``npc_id``, else the nearest of ``npc_type``,
    else the boss in sight for ``fight_boss``) and swing until it is gone;
    a ``fight_boss`` with no boss in sight walks to its door."""
    w, st = ctx.world, ctx.started
    if w.pos is None:
        return Act(None, "fight: unplaced")
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
