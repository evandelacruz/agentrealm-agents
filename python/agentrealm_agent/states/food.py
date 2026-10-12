"""Food a short walk away, priced by Heal's walk (A89, A135): the one food
test.

Heal goes for the food route, and while there is one, out of a fight, the
planner's potion is refused (``use_item.potion_refused``). The candidates
are ``healing.food_near``'s: food lying near and blocks that drop food (a
bush, for berries). Each is priced by its walk on Heal's ground
(``walk_grids``), a block on the way, or the block itself, costed at its
break; one no walk reaches is no route.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Policy
from ..executor.supply_refusals import must_stand_on
from ..healing import food_near
from ..hostile_ground import reach_cells
from ..memory import Memory
from ..navigation import cost_path, path_cost
from ..navigation.planner import CostGridParams
from ..navigation.rejection import navigation_avoid_costly
from .. import targets as targets_mod
from ..pathing import grid_params
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext


@dataclass(frozen=True)
class FoodRoute:
    """Where Heal goes for food: ``food`` lying there, or a ``block`` to
    break there for the food it drops."""

    kind: str
    pos: Pos
    food: Entity | None = None


def ground(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> tuple[set[Pos], set[Pos]]:
    """Heal's ground, as (avoid, costly), for every walk and pick it makes:
    hazards are both, and cells in a known hostile's reach, in view or
    remembered (``hostile_ground.reach_cells``), are costly, so a walk goes
    round a pack instead of through it."""
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    reach = reach_cells(w, policy)
    return nav_avoid | hazards, nav_costly | hazards | reach


def walk_grids(w: WorldModel, ctx: PlayContext, at: Pos) -> tuple[CostGridParams, CostGridParams]:
    """Heal's ground for a walk to ``at``, as the grid it steps on and the
    same grid with each block on the straight way a held item opens priced at
    its break (``grid_params`` ``break_goal``)."""
    m, policy = ctx.memory, ctx.policy
    plan_avoid, plan_costly = ground(w, m, policy, ctx)
    walk = grid_params(policy, plan_avoid, plan_costly)
    opening = grid_params(policy, plan_avoid, plan_costly, m=m, w=w, knowledge=ctx.knowledge, break_goal=at)
    return walk, opening


def take_reach(m: Memory, food: Entity) -> int:
    """How near a ``Take`` of ``food`` is sent from: beside it, or on its own
    cell once a ``Take`` from beside it was refused out of reach."""
    return 0 if must_stand_on(m, food) else 1


def _price(w: WorldModel, ctx: PlayContext, at: Pos, near: int, tie: int) -> tuple[int, int, int] | None:
    """Heal's one price of a walk to ``at``: none within ``near`` of it,
    None when no walk reaches it."""
    here = w.pos
    assert here is not None
    if chebyshev(at, here) <= near:
        return (0, 0, tie)
    _, opening = walk_grids(w, ctx, at)
    way = cost_path(w, at, opening)
    if not way or way[-1] != at:
        return None
    cost = path_cost(w, way, at, opening)
    return None if cost is None else (cost, chebyshev(at, here), tie)


def food_target(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The food lying near Heal goes for, the cheapest by its walk (food in
    reach costs none). The food it went for first stays its target while it
    is one (A71)."""
    m = ctx.memory
    foods, _ = food_near(w, m, ctx.knowledge)
    c = targets_mod.committed(m, w, "heal_food")
    if (kept := next((f for f in foods if c is not None and f.id == c.target), None)) is not None:
        return kept
    targets_mod.release(m, "heal_food")
    priced = [(p, f) for f in foods if (p := _price(w, ctx, f.pos, take_reach(m, f), f.id)) is not None]
    return min(priced, key=lambda pf: pf[0])[1] if priced else None


def block_target(w: WorldModel, ctx: PlayContext) -> Pos | None:
    """The block near Heal breaks for the food it drops, the cheapest by its
    walk (the block priced at its break). The one it went for first stays its
    target while it is one (A71)."""
    m = ctx.memory
    _, blocks = food_near(w, m, ctx.knowledge)
    c = targets_mod.committed(m, w, "heal_block")
    if c is not None and c.target in blocks:
        return c.target
    targets_mod.release(m, "heal_block")
    priced = [(p, b) for b in blocks if (p := _price(w, ctx, b, 0, 0)) is not None]
    return min(priced, key=lambda pb: pb[0])[1] if priced else None


def food_route(w: WorldModel, ctx: PlayContext) -> FoodRoute | None:
    """Where Heal would go for food now: the food lying near it would take,
    else the block it would break for food, else None."""
    if (food := food_target(w, ctx)) is not None:
        return FoodRoute("food", food.pos, food)
    if (block := block_target(w, ctx)) is not None:
        return FoodRoute("block", block)
    return None
