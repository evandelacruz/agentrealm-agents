"""Gather: gems from grass, bushes and gem piles in safe-ish ground (A22)."""

from __future__ import annotations

from ..config import Policy
from ..directives import attack_forbidden
from ..memory import Memory
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import flee_step, grid_params, next_step
from ..plan_goals import gather_gems_goal
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .gather_safe import is_safe_ish
from .intents import set_position, take, use_block, use_on

GATHER_BLOCKS = frozenset({"grass", "bush"})
GEM_PILE_CODES = frozenset({"gem", "gem_pile"})


class GatherState(State):
    name = "Gather"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return _wants_gather(world, ctx) and world.alive and world.pos is not None and ctx.policy.kind == "scripted"

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not _wants_gather(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return gather_outcome(world, ctx.memory, ctx.policy, never_attack=ctx.never_attack, knowledge=ctx.knowledge)


def _wants_gather(world: WorldModel, ctx: PlayContext) -> bool:
    goal = gather_gems_goal(ctx.directives)
    if goal is None:
        return False
    gems = world.gems
    if gems is not None and gems >= goal.count:
        return False
    return True


def _attack_range(w: WorldModel) -> int:
    return max(1, w.attack_range or 1)


def is_gem_pile(e: Entity) -> bool:
    return e.kind == "supply" and e.code in GEM_PILE_CODES


def gather_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    never_attack: list[str],
    knowledge=None,
    state: str = "Gather",
) -> StateOutcome:
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    view = w.view
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    escape: set[Pos] = set()
    if here in hazards:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path, m.gather_block = [], None
            p = min(safe)
            return StateOutcome([set_position(p)], f"off {view.tiles.get(here)}", reflex=True, state=state)
        escape = hazards
    plan_avoid = blocked - escape
    plan_costly = escape | nav_costly

    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight":
            if target.kind == "character" and not attack_forbidden(target, never_attack):
                return StateOutcome(
                    [use_on(target)], f"fight {target.kind} {target.id}", reflex=True, state=state
                )
        away = flee_step(w, hostiles, blocked)
        if away is not None:
            m.path, m.gather_block = [], None
            return StateOutcome(
                [set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=state
            )

    reach = _attack_range(w)
    piles = [
        e
        for e in w.entities
        if is_gem_pile(e) and chebyshev(e.pos, here) <= 1 and is_safe_ish(w, e.pos, policy)
    ]
    if piles:
        s = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id))
        return StateOutcome([take(s)], f"take {s.code or s.id}", reflex=True, state=state)

    if view.tiles.get(here) == "grass" and is_safe_ish(w, here, policy):
        return StateOutcome([use_block(here)], "cut grass", state=state)

    bushes = [
        p
        for p in view.tiles
        if view.tiles[p] == "bush" and chebyshev(p, here) <= reach and is_safe_ish(w, p, policy)
    ]
    if bushes:
        p = min(bushes, key=lambda pos: (chebyshev(pos, here), pos))
        return StateOutcome([use_block(p)], "cut bush", state=state)

    if m.gather_block is not None and not _still_want_block(w, m.gather_block, policy):
        m.gather_block = None
        m.path = []

    step = next_step(w, plan_avoid, m.path)
    if step is None:
        _replan_gather(w, m, policy, plan_avoid, plan_costly, knowledge)
        step = next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"gather → {m.path[-1]}", state=state)

    return StateOutcome(None, "no gather target", state=state)


def _still_want_block(w: WorldModel, block: Pos, policy: Policy) -> bool:
    kind = w.view.tiles.get(block)
    return kind in GATHER_BLOCKS and is_safe_ish(w, block, policy)


def _replan_gather(w: WorldModel, m: Memory, policy: Policy, blocked: set[Pos], costly: set[Pos], knowledge) -> None:
    from ..navigation import cost_path, nearest_target

    m.path, m.gather_block = [], None
    params = grid_params(policy, blocked, costly)
    here = w.pos
    assert here is not None

    piles = [e for e in w.entities if is_gem_pile(e) and is_safe_ish(w, e.pos, policy)]
    if piles:
        target = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id)).pos
        path = cost_path(w, target, params)
        if next_step(w, blocked, path):
            m.path, m.gather_block = path, target
            return

    bush_stands: set[Pos] = set()
    bush_at: dict[Pos, Pos] = {}
    for p, block in w.view.tiles.items():
        if block != "bush" or not is_safe_ish(w, p, policy):
            continue
        for stand in w.neighbours(p):
            if w.view.walkable(stand) and stand not in w.occupied():
                bush_stands.add(stand)
                bush_at[stand] = p
    if bush_stands:
        found = nearest_target(w, bush_stands, params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.gather_block = found[1], bush_at[found[0]]
            return

    grass = {p for p, block in w.view.tiles.items() if block == "grass" and is_safe_ish(w, p, policy)}
    if grass:
        found = nearest_target(w, grass, params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.gather_block = found[1], found[0]
            return
