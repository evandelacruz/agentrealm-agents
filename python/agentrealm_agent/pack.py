"""Pack room: the one rule for making room in a full pack (A102, A20).

Every state that picks a supply up asks :func:`make_room` first: Shop's
``buy``, Loot's ``fetch_item``, Gather's gem piles, the Pickup reflex,
Recover's withdraw, Level's prerequisite and Detour's pick of what is worth
a walk. The rule:

- A supply that fits, or a gem or life (counters, no slot), is taken.
- A supply the plan reserves is never dropped (:func:`reserved_supplies`):
  one held supply for each op on the stack that will use it.
- Nothing is dropped where a dropped supply becomes shop stock: on a known
  shop cell, where a priced supply was seen (``travel.knowledge.iter_shop_cells``),
  or on one in sight now. A supply dropped there came back for sale
  (docs/observations); the Manual does not say so. A pickup that needs a drop
  there steps off first (``MOVE``).
- Which held supply to give up is always the planner's call: no code ranks
  a carried item as useless (a tool unused so far still smashes, burns or
  fights). A ``buy`` or ``fetch_item`` op names it with ``drop``; one that
  names nothing it may drop goes back to the planner (``ASK``) with the
  choices. A pickup no op asked for (the reflexes) is skipped instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .break_memory import capabilities_for_code
from .healing import supply_matches
from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase, knowledge_items
from .loot import Pickup, carry_slots_used, droppable_supplies, inventory_full, is_counter_supply
from .supplies import kept_on_break
from .travel.knowledge import iter_shop_cells
from .world import NEIGHBOURS, Pos, WorldModel, chebyshev

TAKE = "take"  # it fits: Take it
DROP = "drop"  # Drop ``Room.drop`` here, then Take it
MOVE = "move"  # a drop is due, but not on this cell: step off it first
ASK = "ask"  # which supply to drop is the planner's call
SKIP = "skip"  # not worth a slot


@dataclass(frozen=True)
class Room:
    """What making room for one pickup comes to; ``why`` says it for the log and the planner."""

    kind: str
    drop: InventorySupply | None = None
    why: str = ""


def reserved_supplies(w: WorldModel, ops: Iterable[dict[str, Any]], knowledge: KnowledgeBase | None) -> list[InventorySupply]:
    """Held and stowed supplies the plan will use, one per op that needs one:
    ``use_block``'s ``code``, ``equip``'s ``code``, ``fight_boss``'s ``armed``
    and ``worn``, and ``break_block``'s tool for its ``capability`` (none when
    the armed supply has it, one kept for every break when a break does not use
    it up, else one per op). An op is matched to the lowest id not yet reserved."""
    pool = sorted(w.held_supplies + w.chest_supplies, key=lambda s: s.id)
    out: list[InventorySupply] = []

    def claim(fits) -> None:
        taken = {s.id for s in out}
        found = next((s for s in pool if s.id not in taken and fits(s)), None)
        if found is not None:
            out.append(found)

    for op in ops:
        kind = op.get("op")
        if kind in ("use_block", "equip") and isinstance(op.get("code"), str):
            code = op["code"]
            claim(lambda s, c=code: supply_matches(c, s.code))
        elif kind == "fight_boss":
            worn = op.get("worn") if isinstance(op.get("worn"), list) else []
            for code in [op.get("armed"), *worn]:
                if isinstance(code, str) and code:
                    claim(lambda s, c=code: supply_matches(c, s.code))
        elif kind == "break_block":
            cap = op.get("capability")

            def opens(code: str | None, cap=cap) -> bool:
                return bool(code) and cap in capabilities_for_code(code, knowledge)

            if opens(w.armed_code) or any(opens(s.code) and kept_on_break(s.code) for s in out):
                continue
            kept = [s for s in pool if opens(s.code) and kept_on_break(s.code)]
            claim(lambda s: s in kept[:1] if kept else opens(s.code))
    return out


def drop_makes_stock(w: WorldModel, knowledge: KnowledgeBase | None, pos: Pos | None = None) -> bool:
    """A supply dropped at ``pos`` (here by default) would become shop stock."""
    pos = pos if pos is not None else w.pos
    if pos is None:
        return False
    return pos in w.for_sale() or (w.map_id, pos) in set(iter_shop_cells(knowledge))


def make_room(
    w: WorldModel,
    p: Pickup,
    *,
    plan_ops: Iterable[dict[str, Any]] = (),
    knowledge: KnowledgeBase | None = None,
    named: str | None = None,
) -> Room:
    """Whether ``p`` can be taken now, and what to drop for it (the module rule).

    ``plan_ops`` are the ops left on the stack, which reserve what they use;
    ``named`` is the code a ``buy`` or ``fetch_item`` op said to drop."""
    items = knowledge_items(knowledge)
    if (p.chest_id is None and is_counter_supply(p.code, items)) or not inventory_full(w):
        return Room(TAKE)
    reserved = {s.id for s in reserved_supplies(w, plan_ops, knowledge)}
    free = [s for s in droppable_supplies(w) if s.id not in reserved]
    full = f"pack full ({carry_slots_used(w)}/{w.carry_capacity})"
    if not named:
        return Room(ASK, why=f"{full}: name what to drop for {p.code} with drop; {_choices(free)}")
    shed = next((s for s in free if s.code == named), None)
    if shed is None:
        return Room(ASK, why=f"{full}: drop={named} is not held, cannot be dropped, or the plan reserves it; {_choices(free)}")
    if drop_makes_stock(w, knowledge):
        return Room(MOVE, shed, f"{full}: not dropping {shed.code} on a shop cell")
    return Room(DROP, shed, f"drop {shed.code or shed.id} for {p.code}")


def _choices(free: list[InventorySupply]) -> str:
    codes = sorted({s.code for s in free if s.code})
    return f"droppable: {', '.join(codes)}" if codes else "nothing held can be dropped"


def drop_spot(w: WorldModel, knowledge: KnowledgeBase | None, near: Pos) -> Pos | None:
    """A neighbouring cell still in reach of ``near`` where a drop stays ours:
    walkable, free, and not a shop cell."""
    here = w.pos
    if here is None:
        return None
    blocked = w.occupied() | w.for_sale()
    cells = [
        n
        for dx, dy in NEIGHBOURS
        if (n := (here[0] + dx, here[1] + dy)) not in blocked
        and w.view.walkable(n)
        and chebyshev(n, near) <= 1
        and not drop_makes_stock(w, knowledge, n)
    ]
    return min(cells) if cells else None
