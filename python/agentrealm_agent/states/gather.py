"""Gather: carry out the plan's ``gather_gems`` op from grass, bushes and gem piles in safe-ish ground (A22)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path, nearest_target
from ..pathing import grid_params, next_step
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets, safe_default
from .gather_safe import is_safe_ish
from .intents import set_position, take, use_block

# Authored gem piles spawn as ground supplies (Obs, GAME_NOTES.md Gems).
# Gem caches (gem_cache_5/7/10) are a different drop and are not piles.
# A priced supply with the same code is shop stock: taking it is a purchase.
GEM_PILE_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
# Bushes are not walkable, so they are cut from a neighbouring cell: the
# pocket knife's range is 1 (GAME_NOTES.md Olympuff starting kit).
BUSH_REACH = 1
GOAL = "gather"


class GatherState(State):
    """Executor for ``gather_gems``: ``Use`` grass and bushes or ``Take`` gem
    piles in safe-ish ground until the gem counter reaches the op's count.
    With no reachable target in view it explores safe ground for one."""

    name = "Gather"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        out = gather_outcome(world, ctx.memory, ctx.policy, knowledge=ctx.knowledge)
        if out.intents is None:
            out = safe_default(world, ctx)
            out.state, out.reason = self.name, f"look for gems: {out.reason}"
        return out


def is_gem_pile(e: Entity) -> bool:
    """A free ground gem: an authored pile, or one a grass or bush drop left."""
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
    state: str = "Gather",
) -> StateOutcome:
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    view = w.view

    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    piles = [e for e in w.entities if is_gem_pile(e) and chebyshev(e.pos, here) <= 1 and is_safe_ish(w, e.pos, policy)]
    if piles:
        s = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id))
        return StateOutcome([take(s.id)], f"take {s.code or s.id}", state=state)

    if view.tiles.get(here) == "grass" and is_safe_ish(w, here, policy):
        return StateOutcome([use_block(here)], "cut grass", state=state)

    bushes = [
        p for p in view.tiles if view.tiles[p] == "bush" and chebyshev(p, here) <= BUSH_REACH and is_safe_ish(w, p, policy)
    ]
    if bushes:
        p = min(bushes, key=lambda pos: (chebyshev(pos, here), pos))
        return StateOutcome([use_block(p)], "cut bush", state=state)

    # Follow only a path Gather planned, toward a target that still qualifies.
    if m.goal == GOAL and not _still_wanted(w, m.gather_target, policy):
        m.path, m.goal, m.gather_target = [], "", None
    step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is None:
        _replan_gather(w, m, policy, plan_avoid, plan_costly)
        step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is not None:
        return StateOutcome([set_position(step)], f"gather → {m.path[-1]}", state=state)

    return StateOutcome(None, "no gather target", state=state)


def _still_wanted(w: WorldModel, target: tuple[str, Pos] | None, policy: Policy) -> bool:
    if target is None:
        return False
    kind, pos = target
    if kind == "pile":
        return any(is_gem_pile(e) and e.pos == pos for e in w.entities) and is_safe_ish(w, pos, policy)
    return w.view.tiles.get(pos) == kind and is_safe_ish(w, pos, policy)


def _replan_gather(w: WorldModel, m: Memory, policy: Policy, blocked: set[Pos], costly: set[Pos]) -> None:
    """Plan to the nearest pile, then bush, then grass; leave ``m.path`` alone if none.

    A failed plan keeps another state's path.
    """
    if m.goal == GOAL:
        m.path, m.goal, m.gather_target = [], "", None
    params = grid_params(policy, blocked, costly)
    here = w.pos
    assert here is not None

    piles = [e for e in w.entities if is_gem_pile(e) and is_safe_ish(w, e.pos, policy)]
    if piles:
        target = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id)).pos
        path = cost_path(w, target, params)
        if next_step(w, blocked, path):
            m.path, m.goal, m.gather_target = path, GOAL, ("pile", target)
            return

    bush_at: dict[Pos, Pos] = {}
    for p, block in w.view.tiles.items():
        if block != "bush" or not is_safe_ish(w, p, policy):
            continue
        for stand in w.neighbours(p):
            if w.view.walkable(stand) and stand not in w.occupied():
                bush_at.setdefault(stand, p)
    if bush_at:
        found = nearest_target(w, set(bush_at), params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.goal, m.gather_target = found[1], GOAL, ("bush", bush_at[found[0]])
            return

    grass = {p for p, block in w.view.tiles.items() if block == "grass" and is_safe_ish(w, p, policy)}
    if grass:
        found = nearest_target(w, grass, params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.goal, m.gather_target = found[1], GOAL, ("grass", found[0])
