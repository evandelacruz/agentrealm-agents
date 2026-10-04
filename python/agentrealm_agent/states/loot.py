"""Loot: Take, WithdrawFromChest, Drop junk when full; hearts first (A20)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..loot import (
    best_loot_target,
    inventory_full,
    is_counter_supply,
    loot_score,
    needs_drop_for_supply,
    survival_blocks_loot,
    supply_worth_looting,
)
from ..memory import Memory
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import grid_params, nav_search, next_step
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import drop, set_position, take, withdraw_all


class LootState(State):
    name = "Loot"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not ctx.policy.pickup:
            return False
        if not world.alive or world.pos is None:
            return False
        if survival_blocks_loot(world, ctx.policy):
            return False
        items = (ctx.knowledge.items if ctx.knowledge else {}) or {}
        return best_loot_target(world, items) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return loot_outcome(
            world,
            ctx.memory,
            ctx.policy,
            knowledge=ctx.knowledge,
            state=self.name,
        )


def loot_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    knowledge: KnowledgeBase | None = None,
    state: str = "Loot",
) -> StateOutcome:
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    items = (knowledge.items if knowledge else {}) or {}
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    plan_avoid = nav_avoid | hazards
    plan_costly = nav_costly

    near_supplies = [
        e
        for e in w.entities
        if e.kind == "supply" and chebyshev(e.pos, here) <= 1 and supply_worth_looting(e, items)
    ]
    if near_supplies:
        target = max(near_supplies, key=lambda e: (loot_score(e.code, items), -chebyshev(e.pos, here), -e.id))
        drop_item = needs_drop_for_supply(w, target, items)
        if drop_item is not None:
            return StateOutcome(
                [drop(drop_item.id)],
                f"drop {drop_item.code or drop_item.id} for {target.code or target.id}",
                reflex=True,
                state=state,
            )
        if inventory_full(w) and not is_counter_supply(target.code) and needs_drop_for_supply(w, target, items) is None:
            return StateOutcome(None, "carry full", state=state)
        return StateOutcome([take(target)], f"take {target.code or target.id}", reflex=True, state=state)

    chest_id, chest_reason = _adjacent_chest(w, here, items)
    if chest_id is not None:
        return StateOutcome([withdraw_all(chest_id)], chest_reason, reflex=True, state=state)

    if w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, cid = w.death_chest
        if chebyshev(at, here) <= 1 and w.chest_contents.get(cid) is None:
            return StateOutcome(None, f"open chest {cid}", state=state)

    target = best_loot_target(w, items)
    if target is None:
        return StateOutcome(None, "nothing to loot", state=state)

    plan_name = "chest" if target.kind in ("chest", "death_chest") else "loot"
    if m.goal != plan_name or not next_step(w, plan_avoid, m.path):
        found = cost_path(
            w,
            target.pos,
            grid_params(policy, plan_avoid, plan_costly),
            nav=nav_search(m, w, plan_name, target.pos),
        )
        if next_step(w, plan_avoid, found):
            m.path, m.goal = found, plan_name

    step = next_step(w, plan_avoid, m.path)
    if step is not None:
        label = target.kind.replace("_", " ")
        return StateOutcome([set_position(step)], f"{label} → {target.pos}", state=state)

    return StateOutcome(None, "loot unreachable", state=state)


def _adjacent_chest(w: WorldModel, here, items: dict) -> tuple[int | None, str]:
    best: tuple[int, str, int] | None = None
    if w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, cid = w.death_chest
        if chebyshev(at, here) <= 1:
            contents = w.chest_contents.get(cid)
            if contents:
                best = (cid, f"recover from chest {cid}", 10_000)
    for e in w.entities:
        if e.kind != "chest" or chebyshev(e.pos, here) > 1:
            continue
        contents = w.chest_contents.get(e.id)
        if not contents:
            continue
        score = max(loot_score(s.code, items) for s in contents)
        row = (e.id, f"withdraw chest {e.id}", score)
        if best is None or row[2] > best[2]:
            best = row
    if best is None:
        return None, ""
    return best[0], best[1]
