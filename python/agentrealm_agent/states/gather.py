"""Gather: carry out the plan's ``gather_gems`` op from grass and gem piles (A22, A81).

Bushes drop berries, not gems (GAME_NOTES.md Gems), so Gather never cuts
one; food is Heal's and Loot's.

It works any known ground off hazards with no known hostile near
(``gather_ground``), the open field and safe zones alike, but cuts field
cells (not known safe) ahead of safe ones: a cut on town grass was seen to
have no effect (A63 run 2). With nothing to cut while standing on safe
ground it walks out to the nearest known field ground, else the nearest
frontier. Grass in a region our own cuts showed barren is left
alone (A63), unless the op names that region with ``x, y``, and so are cells
it cut too recently to have grown back, cells whose last ``Use`` had no
effect, and regions or safe zones where cuts keep having none
(``GemYieldTracker.uncuttable``).

A target region is worked before anything else: the one the op names with
``x, y``, else, while Gather stands in a poor region (a fair sample, low
yield), the best better region known nearby (``gem_yield.better_region``).
Gather walks to it and cuts only there while it has a cell to cut; poor
regions are left alone while a better one is known. With nothing known to
cut there, a named region it has not seen is walked toward, and otherwise
Gather works as if there were no target (free-play run 2)."""

from __future__ import annotations

from typing import Callable, Iterable

from ..break_memory import capabilities_for_code, pick_supply_for_capability
from ..config import Policy
from ..directives import attack_forbidden
from ..executor import DEFAULT_TICK_RATE_HZ
from ..gem_yield import (
    GATHER_BLOCKS,
    REGION_SIZE,
    GemYieldTracker,
    barren_regions,
    better_region,
    exhausted_cells,
    poor_regions,
    STALL_SECONDS,
    blocks_to_region,
    cut_or_since,
    region_corner,
    region_of,
)
from ..healing import FOOD_CODES, POTION_CODES
from ..hostile_ground import GATHER_HOSTILE_RADIUS, Danger, danger, reach_cells
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path, nearest_target
from ..pathing import grid_params, next_step
from ..plan import GoalOp
from ..survival import is_attacker, is_hostile, recently_attacked, would_lose
from ..world import Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets, safe_default
from .fight import engage
from .gather_safe import gather_ground, route_clear
from .intents import arm, arm_and_use, set_position, take, use_block
from .solve import held_supply

# Authored gem piles spawn as ground supplies (Obs, GAME_NOTES.md Gems).
# Gem caches (gem_cache_5/7/10) are a different drop and are not piles.
# A priced supply with the same code is shop stock: taking it is a purchase.
GEM_PILE_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
CUT = "cut"  # the capability Gather arms for: grass is cut
GOAL = "gather"
OUT = "out"  # ``m.gather_target`` kind: walking off safe ground to field ground or the frontier
OFF = "off"  # ``m.gather_target`` kind: moving off from a hostile that shadows us
REGION = "region"  # ``m.gather_target`` kind: walking toward a target region with no cuttable cell seen yet
# Known targets tried per replan, nearest first: bounds the searches when the
# closest ones turn out unreachable (across water, say).
GATHER_CANDIDATES = 16
# Paths planned per target kind before giving up on that kind, when the
# nearest targets can only be reached through a known hostile's reach.
ROUTE_TRIES = 3
# A hostile within GATHER_HOSTILE_RADIUS this long without hitting us is
# shadowing us: Gather fights it or moves well off (A63 run 3: one followed
# at 4–6 blocks and cutting stalled for 45 s).
SHADOW_SECONDS = 15
# Gather not seeing the shadow near for this long (another state ran, or it
# left) restarts its clock.
SHADOW_GAP_SECONDS = 5
# Moving off goes to cells at least this far from the shadowing hostile, in one walk.
MOVE_OFF_DISTANCE = 2 * GATHER_HOSTILE_RADIUS
# Gather's last decision, for the planner's State (``Memory.gather_status``).
CUTTING = "cutting"
TAKING = "taking a gem"
WALKING = "walking to {}"  # grass, a gem pile
NO_EFFECT = "cuts have no effect here"
HEADING_OUT = "heading out of safe ground"
NONE_CUTTABLE = "no cuttable cell in view"
REGION_BARREN = "region barren"
BLOCKED = "blocked by hostile"
MOVING_OFF = "moving off from a hostile that shadows"
FIGHTING = "fighting a hostile that shadows"
WAITING = "way to {} taken, waiting"
STALLED = "{}, no cut for {} s"
WALK_TARGETS = {"grass": "grass", "pile": "a gem pile", REGION: "a target region"}
IN_REGION = "{} (region {},{})"  # any status while Gather works only in a target region


class GatherState(State):
    """Executor for ``gather_gems``: ``Use`` grass or ``Take`` gem
    piles until the gem counter reaches the op's count, walking to the
    nearest known one when none is in reach, field cells before safe ones.
    A cut goes out with a tool that cuts, armed in the same queue when the
    armed item is not known to cut; the weapon it swapped out is armed again
    once no ``gather_gems`` op is on top.
    Grass in a barren region (unless the op names it), or where
    cuts had no effect, are skipped. On safe ground with nothing to cut, it
    heads out to field ground or the frontier;
    with nowhere to head, it explores (the safe default) to reveal more.

    A hostile that stays near for ``SHADOW_SECONDS`` without hitting us is
    fought when ``on_hostile = fight`` and the win estimate passes, else
    Gather moves well off from it in one walk (A63 run 3)."""

    name = "Gather"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None or ctx.memory.gather_rearm is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        m = ctx.memory
        if op is None:
            return _rearm_weapon(world, m)
        tick_hz = ctx.plan.tick_hz if ctx.plan is not None else DEFAULT_TICK_RATE_HZ
        shadow = shadowing_hostile(world, m, ctx.policy, tick_hz)
        if shadow is not None:
            fight = fight_shadow(world, ctx, shadow)
            if fight is not None:
                m.gather_status = FIGHTING
                return fight
        out = gather_outcome(
            world, m, ctx.policy, knowledge=ctx.knowledge, op=op, gem_cuts=ctx.gem_cuts, shadow=shadow, tick_hz=tick_hz
        )
        if out.intents is not None or out.wait:
            return out
        out = safe_default(world, ctx)
        out.state, out.reason = self.name, f"look for gems: {out.reason}"
        return out


def shadowing_hostile(w: WorldModel, m: Memory, policy: Policy, tick_hz: int) -> Entity | None:
    """The hostile that has stayed within ``GATHER_HOSTILE_RADIUS`` of us for
    ``SHADOW_SECONDS`` without hitting us, else None (A63 run 3).

    Tracks one threat (``is_hostile``) at a time in ``m.gather_shadow``: the
    nearest, until it leaves the radius. Its clock starts again when it hits
    us (the survival states handle that), or when Gather has not seen it near
    for ``SHADOW_GAP_SECONDS``.
    """
    here = w.pos
    if here is None:
        return None
    near = {
        e.id: e for e in w.entities if is_hostile(w, policy, e) and chebyshev(e.pos, here) <= GATHER_HOSTILE_RADIUS
    }
    tracked = m.gather_shadow
    if tracked is None or tracked[0] not in near or w.tick - tracked[2] >= SHADOW_GAP_SECONDS * tick_hz:
        nearest = min(near.values(), key=lambda e: (chebyshev(e.pos, here), e.id), default=None)
        m.gather_shadow = (nearest.id, w.tick, w.tick) if nearest is not None else None
        return None
    e, since = near[tracked[0]], tracked[1]
    if is_attacker(w, e) and w.attacked_tick is not None and w.attacked_tick >= since:
        since = w.attacked_tick  # it hit us: "without hitting us" starts over
    m.gather_shadow = (e.id, since, w.tick)
    if recently_attacked(w) and is_attacker(w, e):
        return None
    return e if w.tick - since >= SHADOW_SECONDS * tick_hz else None


def fight_shadow(w: WorldModel, ctx: PlayContext, e: Entity) -> StateOutcome | None:
    """Close on and swing at the shadowing ``e`` when ``on_hostile = fight``,
    it may be attacked, and the win estimate (counting it as in range) passes;
    else None."""
    policy = ctx.policy
    if policy.on_hostile != "fight" or attack_forbidden(e, ctx.never_attack) or w.pos is None:
        return None
    if would_lose(w, policy, ctx.params, also=e):
        return None
    out = engage(w, ctx, e, GatherState.name)
    if not out.intents:
        return None
    out.reason = f"{e.kind} {e.id} shadows us: {out.reason}"
    return out


def is_gem_pile(e: Entity) -> bool:
    """A free ground gem: an authored pile, or one a grass cut or a kill dropped."""
    return (
        e.kind == "supply"
        and e.code in GEM_PILE_SUPPLY_CODES
        and e.gem_price is None
    )


def gather_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    knowledge: KnowledgeBase | None = None,
    op: GoalOp | None = None,
    gem_cuts: GemYieldTracker | None = None,
    state: str = "Gather",
    shadow: Entity | None = None,
    tick_hz: int = DEFAULT_TICK_RATE_HZ,
) -> StateOutcome:
    """Gather's move this decision, or no intents with nothing to work.

    ``shadow`` is a hostile that shadows us (``shadowing_hostile``): Gather
    walks to ground ``MOVE_OFF_DISTANCE`` from it before anything else.

    Sets ``m.gather_status`` from results, not intents alone. With intents:
    ``HEADING_OUT`` when walking off safe ground; ``MOVING_OFF`` when moving
    off from ``shadow``; ``TAKING`` for a pile ``Take``, ``WALKING`` to a
    pile; else ``NO_EFFECT`` when standing where cuts are known not to work,
    or the latest cut had no effect in this region and none worked since;
    else ``WALKING`` and the target for any other walk; else ``CUTTING``.
    With none, ``BLOCKED`` when known grass is barred only by a
    hostile near it, ``REGION_BARREN`` when standing in a skipped barren
    region, else ``NONE_CUTTABLE``. Any status but ``CUTTING`` adds how long
    it has been when no cut has taken effect for ``STALL_SECONDS`` (``STALLED``).
    """
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    skip, target, named = _regions(w, knowledge, op)
    d = danger(w, policy, bool(op.get("fight")) if op is not None else False)
    safe = safe_tiles(w, w.map_id) if w.map_id is not None else set()
    dead_regions, dead_cells = gem_cuts.uncuttable(w, safe) if gem_cuts is not None else (set(), set())
    exhausted = exhausted_cells(knowledge, w.map_id, w.tick, gem_cuts) | dead_cells
    out, worked = _gather_step(
        w, m, policy, here, skip | dead_regions, exhausted, safe, knowledge, state, shadow, target, named, d
    )
    walk = m.gather_target[0] if m.gather_target is not None and m.goal == GOAL else None
    if out.wait and m.gather_target is not None:
        m.gather_status = WAITING.format(WALK_TARGETS.get(m.gather_target[0], "a cell to cut"))
    elif out.intents is None:
        if _barred_by_hostile(w, policy, skip | dead_regions, exhausted, d):
            m.gather_status = BLOCKED
        else:
            m.gather_status = REGION_BARREN if region_of(here) in skip else NONE_CUTTABLE
    elif out.intents[0].get("verb") == "SetPosition" and walk == OUT:
        m.gather_status = HEADING_OUT
    elif out.intents[0].get("verb") == "SetPosition" and walk == OFF:
        m.gather_status = MOVING_OFF
    elif out.intents[0].get("verb") == "Take":
        m.gather_status = TAKING
    elif walk == "pile":
        m.gather_status = WALKING.format(WALK_TARGETS["pile"])
    elif here in dead_cells or region_of(here) in dead_regions or (
        gem_cuts is not None and gem_cuts.no_effect_near(w.map_id, here, w.tick)
    ):
        m.gather_status = NO_EFFECT
    elif out.intents[0].get("verb") == "SetPosition":
        m.gather_status = WALKING.format(WALK_TARGETS.get(walk or "", "a cell to cut"))
    else:
        m.gather_status = CUTTING
    _note_in_region(w, m, target, tick_hz)
    idle = _seconds_without_cut(w, m, gem_cuts, tick_hz)
    if m.gather_status != CUTTING and idle >= STALL_SECONDS:
        m.gather_status = STALLED.format(m.gather_status, idle)
    if worked is not None:
        m.gather_status = IN_REGION.format(m.gather_status, *region_corner(worked))
        out.reason = IN_REGION.format(out.reason, *region_corner(worked))
    return out


def _note_in_region(w: WorldModel, m: Memory, target: tuple[int, int] | None, tick_hz: int) -> None:
    """Keep ``Memory.gather_in_region``: the tick Gather last got nearer its
    target region, or arrived in it. A walk that keeps closing in is never
    a stall; one that is blocked, or Gather in the region with no cut, is.
    ``STALL_SECONDS`` without working the region starts it over."""
    if target is None or w.pos is None:
        return
    d = blocks_to_region(w.pos, target)
    rec = m.gather_in_region
    if rec is None or rec[0] != target or w.tick - rec[3] >= STALL_SECONDS * tick_hz or d < rec[1]:
        rec = (target, d, w.tick, w.tick)
    m.gather_in_region = (target, rec[1], rec[2], w.tick)


def _seconds_without_cut(w: WorldModel, m: Memory, gem_cuts: GemYieldTracker | None, tick_hz: int) -> int:
    """Seconds since the latest cut that took effect, or since this spell of
    Gather began when later. A gap of ``STALL_SECONDS`` between two Gather
    decisions (another op or state ran) starts a new spell."""
    since, seen = m.gather_spell or (w.tick, w.tick)
    if w.tick - seen >= STALL_SECONDS * tick_hz:
        since = w.tick
    m.gather_spell = (since, w.tick)
    return max(0, w.tick - cut_or_since(gem_cuts, since)) // max(1, tick_hz)


def _barred_by_hostile(
    w: WorldModel, policy: Policy, skip: set[tuple[int, int]], exhausted: set[Pos], d: Danger | None = None
) -> bool:
    """Known grass Gather would cut, but for a hostile near it, in view or remembered."""
    if not any(is_hostile(w, policy, e) for e in w.entities) and not any(
        is_hostile(w, policy, s.entity) for s in w.sightings.values()
    ):
        return False
    return any(
        block in GATHER_BLOCKS
        and p not in exhausted
        and region_of(p) not in skip
        and block not in policy.avoid_blocks
        and not gather_ground(w, p, policy, d)
        for p, block in w.view.tiles.items()
    )


def _gather_step(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    here: Pos,
    skip: set[tuple[int, int]],
    exhausted: set[Pos],
    safe: set[Pos],
    knowledge: KnowledgeBase | None,
    state: str,
    shadow: Entity | None = None,
    target: tuple[int, int] | None = None,
    named: bool = False,
    d: Danger | None = None,
) -> tuple[StateOutcome, tuple[int, int] | None]:
    """Gather's move, and the target region it works only in (None when it
    works anywhere).

    While Gather works only in a target region, it walks to gem piles only
    there (free-play run 5: piles outside the named region drew it to a
    hostile's post); one in reach is taken wherever it lies."""
    view = w.view
    cuttable = {
        p
        for p, block in view.tiles.items()
        if block in GATHER_BLOCKS
        and p not in exhausted
        and region_of(p) not in skip
        and gather_ground(w, p, policy, d)
    }
    worked = None
    if target is not None:
        there = {p for p in cuttable if region_of(p) == target}
        if there:
            cuttable, worked = there, target
        elif named and shadow is None and region_of(here) != target and not _seen_region(w, target):
            out = _walk_to_region(w, m, policy, knowledge, target, state, d)
            if out is not None:
                return out, target
    # Field cells first; safe ones only when no field cell is left to cut.
    preferred = {p for p in cuttable if p not in safe} or cuttable
    out = _gather_cells(w, m, policy, here, preferred, cuttable, safe, knowledge, state, shadow, d, worked)
    return out, worked


def _gather_cells(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    here: Pos,
    preferred: set[Pos],
    cuttable: set[Pos],
    safe: set[Pos],
    knowledge: KnowledgeBase | None,
    state: str,
    shadow: Entity | None,
    d: Danger | None = None,
    pile_region: tuple[int, int] | None = None,
) -> StateOutcome:
    """Take or cut what is in reach, else walk to the committed target (A71),
    else to the nearest of ``preferred``.

    The target (``m.gather_target``) is Gather's commitment: it is kept until
    it is reached, gone (cut, taken), proven out of reach, or found unsafe (in
    a known hostile's reach, or reached only through one), even when another
    state walked in between or another cell became nearer (``_still_wanted``,
    ``_replan_gather``). ``d`` is the decision's ``Danger``: remembered
    hostiles' ground, and whether the op chose to fight for it;
    ``pile_region`` bounds the piles it walks to; one in reach is taken anywhere."""
    view = w.view

    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    if shadow is not None:
        off = _move_off(w, m, policy, shadow, plan_avoid, plan_costly, preferred, state, d)
        if off is not None:
            return off
    piles = [
        e
        for e in w.entities
        if is_gem_pile(e)
        and chebyshev(e.pos, here) <= 1
        and gather_ground(w, e.pos, policy, d)
    ]
    if piles:
        _end_walk_out(m)
        s = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id))
        return StateOutcome([take(s.id)], f"take {s.code or s.id}", state=state)

    if view.tiles.get(here) == "grass" and here in preferred:
        _end_walk_out(m)
        return _cut(w, m, knowledge, here, "cut grass", state)

    # A target that no longer qualifies is let go, whoever walked last.
    if not _still_wanted(w, m.gather_target, policy, cuttable, here in safe, d, pile_region):
        if m.goal == GOAL:
            m.path, m.goal = [], ""
        m.gather_target = None
    if m.goal == GOAL and not route_clear(w, policy, m.path, d):
        m.path, m.goal = [], ""  # the way ahead entered a known reach: a way round, or the target goes
    step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is None and _replan_gather(w, m, policy, plan_avoid, plan_costly, preferred, safe, d, pile_region):
        return StateOutcome(None, f"gather → {m.gather_target[1]}: way taken, waiting", state=state, wait=True, progress=False)
    step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is not None:
        return StateOutcome([set_position(step)], f"gather → {m.path[-1]}", state=state)

    return StateOutcome(None, "no gather target", state=state)


def _cut(w: WorldModel, m: Memory, knowledge: KnowledgeBase | None, p: Pos, reason: str, state: str) -> StateOutcome:
    """``Use`` on ``p`` with a tool that cuts: when what is armed is not known
    to cut (a potion a drink left armed, say), arm a held one that is, in the
    same paced queue. With none held, cut with what is in hand."""
    if CUT in capabilities_for_code(w.armed_code or "", knowledge):
        return StateOutcome([use_block(p)], reason, state=state)
    tool = pick_supply_for_capability(w, CUT, knowledge)
    if tool is None or tool.id < 0:
        return StateOutcome([use_block(p)], reason, state=state)
    queue = arm_and_use(w, m, tool.id, use_block(p))
    if not queue:  # the cut's cooldown leaves no room for the Arm and the Use together
        return StateOutcome(None, f"wait out the cooldown to arm {tool.code}, {reason}", state=state, wait=True, progress=False)
    if m.gather_rearm is None and w.armed_code and w.armed_code not in FOOD_CODES | POTION_CODES:
        m.gather_rearm = (w.armed_code, tool.code)  # food or a potion is Heal's to put back
    return StateOutcome(queue, f"arm {tool.code}, {reason}", state=state, paced=True)


def _rearm_weapon(w: WorldModel, m: Memory) -> StateOutcome:
    """Arm the weapon a cut swapped out, once no ``gather_gems`` op is on top.

    Sent once, and only while the cutting tool is still armed: something
    else arming since (Equip's upgrade, say) is not undone.
    """
    weapon, tool = m.gather_rearm or ("", "")
    m.gather_rearm = None
    supply = held_supply(w, weapon) if w.armed_code == tool else None
    if supply is None:
        return StateOutcome(None, "no gather op", state=GatherState.name)
    return StateOutcome([arm(supply.id)], f"re-arm {weapon}", state=GatherState.name)


def _move_off(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    shadow: Entity,
    blocked: set[Pos],
    costly: set[Pos],
    preferred: set[Pos],
    state: str,
    d: Danger | None = None,
) -> StateOutcome | None:
    """One step of a single walk to ground ``MOVE_OFF_DISTANCE`` from ``shadow``,
    grass there first, rather than inching to the nearest cell it does not
    cover (A63 run 3). None once there, or with nowhere to go: the shadow's
    clock restarts and Gather carries on. It moves off onto no other known
    hostile's ground, and by a route clear of its reach, as Gather's other
    walks do (free-play run 7).
    """
    here = w.pos
    assert here is not None
    d = d or danger(w, policy)
    if not (m.goal == GOAL and m.gather_target is not None and m.gather_target[0] == OFF):
        m.path, m.goal, m.gather_target = [], "", None
        reach = set() if d.fight else reach_cells(w, policy, d)
        params = grid_params(policy, blocked, costly | reach)
        shut = blocked | reach
        far = {p for p in w.view.tiles if chebyshev(p, shadow.pos) >= MOVE_OFF_DISTANCE and w.view.walkable(p) and p not in shut}
        for cells in ({p for p in far if p in preferred}, far):
            found = _nearest_clear(w, cells, params, blocked, lambda path: route_clear(w, policy, path, d))
            if found:
                m.path, m.goal, m.gather_target = found[1], GOAL, (OFF, found[0])
                break
    step = next_step(w, blocked, m.path) if m.goal == GOAL else None
    if step is None or m.gather_target is None or m.gather_target[0] != OFF:
        if m.goal == GOAL:
            m.path, m.goal, m.gather_target = [], "", None
        m.gather_shadow = None
        return None
    return StateOutcome([set_position(step)], f"move off {shadow.kind} {shadow.id} → {m.path[-1]}", state=state)


def _end_walk_out(m: Memory) -> None:
    """Something to cut or take turned up: a walk out of safe ground is over."""
    if m.goal == GOAL and m.gather_target is not None and m.gather_target[0] == OUT:
        m.path, m.goal, m.gather_target = [], "", None


def _regions(
    w: WorldModel, knowledge: KnowledgeBase | None, op: GoalOp | None
) -> tuple[set[tuple[int, int]], tuple[int, int] | None, bool]:
    """The regions Gather skips, its target region, if any, and whether the op named it.

    Skipped: barren regions of this map (A63), and poor ones while a better
    region is known nearby. The target is the region the op names with
    ``x, y``, never skipped; else, standing in a poor region, the better one.
    """
    skip = barren_regions(knowledge, w.map_id)
    if op is not None and "x" in op and "y" in op:
        named = region_of((op["x"], op["y"]))
        skip.discard(named)
        return skip, named, True
    assert w.pos is not None
    better = better_region(knowledge, w.map_id, w.pos)
    if better is None:
        return skip, None, False
    poor = poor_regions(knowledge, w.map_id)
    return skip | poor, better if region_of(w.pos) in poor else None, False


def _seen_region(w: WorldModel, region: tuple[int, int]) -> bool:
    """Any cell of ``region`` known."""
    x0, y0 = region_corner(region)
    tiles = w.view.tiles
    return any((x0 + dx, y0 + dy) in tiles for dx in range(REGION_SIZE) for dy in range(REGION_SIZE))


def _walk_to_region(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    region: tuple[int, int],
    state: str,
    d: Danger | None = None,
) -> StateOutcome | None:
    """A step toward the middle of a named region no cell of which is known yet.

    Like every Gather walk, it prices known hostiles' reach as costly and
    takes no path that still crosses it, unless ``d.fight`` (free-play run
    7: Gather walked toward a pack); a kept path whose way ahead comes to
    cross it is planned again."""
    d = d or danger(w, policy)
    x0, y0 = region_corner(region)
    middle = (x0 + REGION_SIZE // 2, y0 + REGION_SIZE // 2)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    kept = m.goal == GOAL and m.gather_target == (REGION, middle) and next_step(w, plan_avoid, m.path)
    if not (kept and route_clear(w, policy, m.path, d)):
        if m.goal == GOAL:
            m.path, m.goal, m.gather_target = [], "", None
        costly = plan_costly if d.fight else plan_costly | reach_cells(w, policy, d)
        path = cost_path(w, middle, grid_params(policy, plan_avoid, costly))
        if not next_step(w, plan_avoid, path) or not route_clear(w, policy, path, d):
            return None
        m.path, m.goal, m.gather_target = path, GOAL, (REGION, middle)
    step = next_step(w, plan_avoid, m.path)
    return StateOutcome([set_position(step)], f"gather → region {x0},{y0}", state=state) if step is not None else None


def _still_wanted(
    w: WorldModel,
    target: tuple[str, Pos] | None,
    policy: Policy,
    cuttable: set[Pos],
    on_safe: bool,
    d: Danger | None = None,
    pile_region: tuple[int, int] | None = None,
) -> bool:
    """The committed target still exists and may still be worked (A71).

    Never a comparison with other cells: a grass cell stays wanted while it
    is cuttable at all, even once a field cell or a pile turns up nearer. A
    pile out of sight is kept until a look at its cell shows it gone, while
    it lies in ``pile_region`` (when set) and on ``gather_ground``: a hit
    taken on the way that shows its ground held by a hostile drops it.
    """
    if target is None:
        return False
    kind, pos = target
    if kind == "pile":
        here = w.pos
        seen = here is not None and chebyshev(pos, here) <= w.perception
        there = any(is_gem_pile(e) and e.pos == pos for e in w.entities)
        return (there or not seen) and _pile_in(pos, pile_region) and gather_ground(w, pos, policy, d)
    if kind == REGION:
        return False  # replanned each decision by ``_walk_to_region`` while its region is unseen
    if kind == OUT:
        # Kept while on safe ground: it was planned because no cut was
        # reachable, so re-checking ``preferred`` each tick would only replan
        # it (A15). Off safe ground the field is in view: replan to cut it.
        return on_safe
    return w.view.tiles.get(pos) == kind and pos in cuttable


# Target kinds ``_replan_gather`` walks back to once another state took the path.
KEPT_KINDS = ("pile", "grass", OUT)
# Gather waits this long for a taken first step toward its target (an
# occupant), then gives the target up (5 s at 10 ticks/s, as a walk's fog hold).
HOLD_TICKS = 50


def _replan_gather(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    preferred: set[Pos],
    safe: set[Pos],
    d: Danger | None = None,
    pile_region: tuple[int, int] | None = None,
) -> bool:
    """Plan to the committed target (``m.gather_target``) while one is kept,
    else pick: the nearest pile (in ``pile_region`` when set), then the
    nearest grass by walk (``preferred``: field
    cells before safe ones), then, on safe ground,
    out to field ground or the frontier; leave ``m.path`` alone if none.

    Unless ``d.fight``, every walk prices known hostiles' reach as costly
    (``reach_cells``) and a target whose path still crosses it is not taken
    (``route_clear``): a kept one is let go at once, and the next nearest is
    tried, up to ``ROUTE_TRIES`` per kind (free-play run 5).

    A kept target is given up only when no path reaches it at all, or its
    path has started on a taken cell for ``HOLD_TICKS``; until then it is
    held, and True says so: the caller waits instead of letting the safe
    default walk away and back (A71). The
    nearest ``GATHER_CANDIDATES`` of each kind are tried. A failed plan
    keeps another state's path.
    """
    d = d or danger(w, policy)
    params = grid_params(policy, blocked, costly if d.fight else costly | reach_cells(w, policy, d))
    here = w.pos
    assert here is not None

    def clear(path: list[Pos]) -> bool:
        return route_clear(w, policy, path, d)

    kept = m.gather_target
    if m.goal == GOAL:
        m.path, m.goal = [], ""
    m.gather_target = None
    if kept is not None and kept[0] in KEPT_KINDS:
        path = _path_to(w, kept, params)
        if path and not clear(path):
            path = None  # unsafe: a valid reason to drop it (A71)
        if path and next_step(w, blocked, path):
            m.path, m.goal, m.gather_target = path, GOAL, kept
            m.gather_hold = None
            return False
        if path:
            if m.gather_hold is None or m.gather_hold[0] != kept:
                m.gather_hold = (kept, w.tick)
            if w.tick - m.gather_hold[1] < HOLD_TICKS:
                m.gather_target = kept
                return True
        m.gather_hold = None  # no way there, or the way stayed taken: pick again

    piles = [
        e for e in w.entities if is_gem_pile(e) and _pile_in(e.pos, pile_region) and gather_ground(w, e.pos, policy, d)
    ]
    for pile in sorted(piles, key=lambda e: (chebyshev(e.pos, here), e.id))[:ROUTE_TRIES]:
        path = cost_path(w, pile.pos, params)
        if next_step(w, blocked, path) and clear(path):
            m.path, m.goal, m.gather_target = path, GOAL, ("pile", pile.pos)
            return False

    found = _nearest_clear(w, set(preferred), params, blocked, clear)
    if found:
        m.path, m.goal, m.gather_target = found[1], GOAL, ("grass", found[0])
        return False

    if here in safe:
        _plan_out(w, m, policy, blocked, params, safe, clear, d)
    return False


def _pile_in(pos: Pos, region: tuple[int, int] | None) -> bool:
    """A pile at ``pos`` lies in ``region``, or no region bounds the piles."""
    return region is None or region_of(pos) == region


def _nearest_clear(
    w: WorldModel, cells: set[Pos], params, blocked: set[Pos], clear: Callable[[list[Pos]], bool]
) -> tuple[Pos, list[Pos]] | None:
    """The nearest of ``cells`` a path reaches with a first step open and a
    ``clear`` route, and that path; up to ``ROUTE_TRIES`` searches."""
    here = w.pos
    assert here is not None
    cells = set(cells)
    for _ in range(ROUTE_TRIES):
        found = nearest_target(w, _nearest(here, cells), params) if cells else None
        if not found or not next_step(w, blocked, found[1]):
            return None
        if clear(found[1]):
            return found
        cells.discard(found[0])
    return None


def _path_to(w: WorldModel, target: tuple[str, Pos], params) -> list[Pos] | None:
    """A path onto a kept target; None when nothing reaches it."""
    found = nearest_target(w, {target[1]}, params)
    return found[1] if found and found[1] else None


def _plan_out(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    params,
    safe: set[Pos],
    clear: Callable[[list[Pos]], bool],
    d: Danger | None = None,
) -> None:
    """Head off safe ground: to the nearest known walkable field cell, else the
    nearest frontier, by a ``clear`` route (``_nearest_clear``)."""
    here = w.pos
    assert here is not None
    view = w.view
    field = {p for p in view.tiles if p not in safe and view.walkable(p) and gather_ground(w, p, policy, d)}
    frontier = {p for p in view.frontier() if p != here and gather_ground(w, p, policy, d)}
    for cells in (field, frontier):
        found = _nearest_clear(w, cells, params, blocked, clear)
        if found:
            m.path, m.goal, m.gather_target = found[1], GOAL, (OUT, found[0])
            return


def _nearest(here: Pos, cells: Iterable[Pos]) -> set[Pos]:
    """The ``GATHER_CANDIDATES`` cells closest to ``here`` (ties to the smaller cell)."""
    return set(sorted(cells, key=lambda p: (chebyshev(p, here), p))[:GATHER_CANDIDATES])
