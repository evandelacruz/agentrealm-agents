"""Recover: walk to the death chest when its spot is safe (A11, A7)."""

from __future__ import annotations

from ..config import Policy
from ..item_table import InventorySupply
from ..knowledge_base import KnowledgeBase
from ..loot import carry_slots_used, loot_score, worst_droppable
from ..memory import Memory
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, nav_search, next_step
from ..world import NEIGHBOURS, MapView, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome, scripted_outcome
from .intents import drop, set_position, withdraw, withdraw_all
from .pickup import knowledge_items


def _map_view(w: WorldModel, map_id: int):
    if w.map_id == map_id:
        return w.view
    return w.maps.get(map_id, MapView())


def recover_approach_target(w: WorldModel, map_id: int, chest_at: Pos) -> Pos | None:
    """A standable cell from which the chest is in reach, known safe via get_zone (A7)."""
    known = w.zones.get(map_id, {})
    view = _map_view(w, map_id)
    safe = safe_tiles(w, map_id)
    if chest_at in known and known[chest_at].safe and view.walkable(chest_at):
        return chest_at
    options = [
        n
        for dx, dy in NEIGHBOURS
        if (n := (chest_at[0] + dx, chest_at[1] + dy)) in safe and view.walkable(n)
    ]
    return min(options) if options else None


def recover_spot_safe(w: WorldModel, map_id: int, chest_at: Pos) -> bool:
    return recover_approach_target(w, map_id, chest_at) is not None


def _chest_supplies(contents: list) -> list[InventorySupply]:
    out: list[InventorySupply] = []
    for entry in contents:
        if isinstance(entry, InventorySupply):
            out.append(entry)
        elif isinstance(entry, int):
            out.append(InventorySupply(entry))
    return out


def death_chest_recover_intents(
    w: WorldModel, chest_id: int, contents: list, items: dict
) -> list[dict] | None:
    """``WithdrawFromChest`` when there is room; ``Drop`` junk when full but worth it; else skip (A20).

    When only some fit, withdraw the best by id: a bare withdraw takes the
    lowest ids first (B117), which could be junk instead of what the drop was for.
    """
    supplies = _chest_supplies(contents)
    if not supplies:
        return None
    room = w.carry_capacity - carry_slots_used(w)
    if room >= len(supplies):
        return [withdraw_all(chest_id)]
    if room > 0:
        best_first = sorted(supplies, key=lambda s: (-loot_score(s.code, items), s.id))
        return [withdraw(chest_id, [s.id for s in best_first[:room]])]
    shed = worst_droppable(w, items)
    if shed is None:
        return None
    best = max(loot_score(s.code, items) for s in supplies)
    if best <= loot_score(shed.code, items):
        return None
    return [drop(shed.id)]


def recover_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    plan_avoid: set[Pos],
    plan_costly: set[Pos],
    *,
    knowledge: KnowledgeBase | None = None,
    state: str = "Recover",
) -> StateOutcome | None:
    """Path to the death chest and withdraw when adjacent (A11, reflex 4b).

    Only the destination is checked for safety; the route is not (A9's).
    None when no step toward the chest can be planned, so the caller yields.
    """
    here = w.pos
    if here is None or w.death_chest is None:
        return None
    map_id, at, chest_id = w.death_chest
    if map_id != w.map_id:
        return None
    target = recover_approach_target(w, map_id, at)
    if target is None:
        return None

    if chebyshev(at, here) <= 1:
        contents = w.chest_contents.get(chest_id)
        if contents:
            items = knowledge_items(knowledge)
            intents = death_chest_recover_intents(w, chest_id, contents, items)
            if intents is not None:
                verb = intents[0]["verb"]
                reason = (
                    f"recover from chest {chest_id}"
                    if verb == "WithdrawFromChest"
                    else f"drop for chest {chest_id}"
                )
                return StateOutcome(intents, reason, reflex=True, state=state)
        if contents is None:
            return StateOutcome(None, f"open chest {chest_id}", state=state)

    if m.goal != "chest" or not next_step(w, plan_avoid, m.path):
        found = cost_path(
            w,
            target,
            grid_params(policy, plan_avoid, plan_costly, m=m),
            nav=nav_search(m, w, "chest", target),
        )
        if next_step(w, plan_avoid, found):
            nav_stuck.track_plan(m, w, "chest", target)
            m.path, m.goal = found, "chest"

    step = next_step(w, plan_avoid, m.path) if m.goal == "chest" else None
    if step is not None:
        return StateOutcome([set_position(step)], f"chest → {m.path[-1]}", state=state)
    return None


def _skip_reason(w: WorldModel) -> str:
    """Why Recover fell back to Explore: out of reach, or in reach with nothing worth taking."""
    _, at, chest_id = w.death_chest
    if w.pos is not None and chebyshev(at, w.pos) <= 1 and w.chest_contents.get(chest_id):
        return f"chest {chest_id} not worth a slot"
    return "chest not reachable"


class RecoverState(State):
    """Priority 3: above Explore. Escape, Retreat and Flee outrank it; the fight and pickup reflexes still run first (PLAN.md)."""

    name = "Recover"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        policy = ctx.policy
        if policy.kind != "scripted" or not policy.pickup or not world.alive or world.pos is None:
            return False
        if world.death_chest is None or world.death_chest[0] != world.map_id:
            return False
        _, at, _ = world.death_chest
        return recover_spot_safe(world, world.map_id, at)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        _, plan_avoid, plan_costly = plan_sets(world, m, policy, ctx.knowledge)
        reflex = reflex_outcome(
            world, policy, never_attack=ctx.never_attack, state=self.name, knowledge=ctx.knowledge
        )
        if reflex is not None:
            return reflex
        out = recover_outcome(
            world, m, policy, plan_avoid, plan_costly, knowledge=ctx.knowledge, state=self.name
        )
        if out is not None:
            return out
        # No step toward the chest: fall back to Explore's goals this round.
        fallback = scripted_outcome(
            world, m, policy, ctx.rng, never_attack=ctx.never_attack, knowledge=ctx.knowledge, plan=ctx.plan, state=self.name
        )
        fallback.reason = f"{_skip_reason(world)}; {fallback.reason}"
        return fallback
