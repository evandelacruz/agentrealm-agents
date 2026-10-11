"""Recover: walk to the death chest when its spot is safe (A11, A7)."""

from __future__ import annotations

from ..config import Policy
from ..item_table import InventorySupply
from ..knowledge_base import KnowledgeBase, knowledge_items
from ..loot import carry_slots_used, loot_score
from ..memory import Memory
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, guided_step, nav_search
from ..world import NEIGHBOURS, MapView, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets
from .intents import set_position, withdraw, withdraw_all


def _map_view(w: WorldModel, map_id: int):
    if w.map_id == map_id:
        return w.view
    return w.maps.get(map_id, MapView())


def recover_approach_target(w: WorldModel, map_id: int, chest_at: Pos) -> Pos | None:
    """A standable cell from which the chest is in reach, known safe via a zone or terrain read (A7)."""
    view = _map_view(w, map_id)
    safe = safe_tiles(w, map_id)
    if chest_at in safe and view.walkable(chest_at):
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
    """``WithdrawFromChest`` when there is room; with a full pack, skip: what to
    give up is the planner's call, never a reflex's (``pack``, A102; A20).

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
    return None


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
                return StateOutcome(intents, f"recover from chest {chest_id}", reflex=True, state=state)
        if contents is None:
            return StateOutcome(None, f"open chest {chest_id}", state=state, wait=True)

    def params():
        return grid_params(policy, plan_avoid, plan_costly, m=m)

    def plan(att):
        return cost_path(w, target, params(), nav=nav_search(m, w, "chest", target))

    # Stuck detection and escalation drive the walk; a chest given up on is backed off (A15).
    step = guided_step(m, w, "chest", target, plan_avoid, plan, knowledge, params=params)
    if step is not None:
        note = nav_stuck.level_note(nav_stuck.active(m, w))
        return StateOutcome([set_position(step)], f"chest → {target}{note}", state=state)
    return None


def _skip_reason(w: WorldModel) -> str:
    """Why Recover fell back to Explore: out of reach, or in reach with nothing worth taking."""
    _, at, chest_id = w.death_chest
    if w.pos is not None and chebyshev(at, w.pos) <= 1 and w.chest_contents.get(chest_id):
        return f"chest {chest_id} not worth a slot"
    return "chest not reachable"


class RecoverState(State):
    """Reflex, after Flee and Pickup: walks to the death chest when its spot is safe."""

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
        out = recover_outcome(
            world, m, policy, plan_avoid, plan_costly, knowledge=ctx.knowledge, state=self.name
        )
        if out is not None:
            return out
        if nav_stuck.awaiting_break(m, world, "chest"):
            return StateOutcome(None, "chest walk stuck: break", state=self.name)
        # No step toward the chest: the next state has the round.
        return StateOutcome(None, _skip_reason(world), state=self.name)
