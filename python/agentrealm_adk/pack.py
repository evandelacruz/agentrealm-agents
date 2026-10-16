"""Pack room: the one rule for making room in a full pack (A102, A20).

Every state that picks a supply up into a slot asks :func:`make_room` first:
Shop's ``buy``, Loot's ``fetch_item``, the Pickup reflex, Level's
prerequisite and Detour's pick of what is worth a walk (Gather takes only
gems, which take no slot; Recover withdraws only what fits). The rule:

- A supply that fits, a gem or life (counters, no slot), or food (eaten on
  pickup, never carried: Manual §11 Food and potions, A155) is taken.
- A supply the plan reserves is never dropped (:func:`reserved_supplies`):
  one held supply for each op on the stack that will use it.
- A price belongs to the supply (Manual §11, A158): a priced subtype costs
  its ``gem_price`` wherever a copy lies, our own drop included, so a drop of
  one forfeits its price on any cell, a shop cell no different. The drop
  says so (``forfeit``), and the choices name each priced one.
- Which held supply to give up is always the planner's call: no code ranks
  a carried item as useless (a tool unused so far still smashes, burns or
  fights). A ``buy`` or ``fetch_item`` op names it with ``drop``; one that
  names nothing it may drop goes back to the planner (``ASK``) with the
  choices. A pickup no op asked for (the reflexes) is skipped instead.

``states.room`` carries a ``Room`` out for the states.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .break_memory import capabilities_for_code
from .fragments import compose_supply_ids
from .healing import supply_matches
from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase, knowledge_items
from .loot import Pickup, carry_slots_used, droppable_supplies, inventory_full, is_counter_supply
from .supplies import eaten_on_pickup, kept_on_break
from .supplies import row as supply_row
from .world import WorldModel

RoomKind = Literal["take", "drop", "ask"]
TAKE: RoomKind = "take"  # it fits: Take it
DROP: RoomKind = "drop"  # Drop ``Room.drop`` here, then Take it
ASK: RoomKind = "ask"  # which supply to drop is the planner's call


@dataclass(frozen=True)
class Room:
    """What making room for one pickup comes to; ``why`` says it for the log
    and the planner. ``drop`` is set exactly when ``kind`` is DROP."""

    kind: RoomKind
    drop: InventorySupply | None = None
    why: str = ""

    def __post_init__(self) -> None:
        assert (self.drop is not None) == (self.kind == DROP), self


def reserved_supplies(w: WorldModel, ops: Iterable[dict[str, Any]], knowledge: KnowledgeBase | None) -> list[InventorySupply]:
    """Held and stowed supplies the plan will use, one per op that needs one:
    ``use_block``'s, ``equip``'s and ``use_item``'s ``code``, ``fight_boss``'s ``armed``
    and ``worn``, ``compose``'s held fragments (one per slot, from what is held,
    as Solve composes them), and ``break_block``'s tool for its ``capability`` (none when the
    armed supply has it, one kept for every break when a break does not use it
    up, else one per op). An op is matched to the lowest id not yet reserved."""
    pool = sorted(w.held_supplies + w.chest_supplies, key=lambda s: s.id)
    out: list[InventorySupply] = []

    def claim(fits) -> None:
        taken = {s.id for s in out}
        found = next((s for s in pool if s.id not in taken and fits(s)), None)
        if found is not None:
            out.append(found)

    for op in ops:
        kind = op.get("op")
        if kind in ("use_block", "equip", "use_item") and isinstance(op.get("code"), str):
            code = op["code"]
            claim(lambda s, c=code: supply_matches(c, s.code))
        elif kind == "compose" and isinstance(op.get("composes_into"), str):
            taken = {s.id for s in out}
            held = sorted((s for s in w.held_supplies if s.id not in taken), key=lambda s: s.id)
            ids = set(compose_supply_ids(held, op["composes_into"]))
            out.extend(s for s in held if s.id in ids)
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
    if (p.chest_id is None and is_counter_supply(p.code, items)) or eaten_on_pickup(p.code) or not inventory_full(w):
        return Room(TAKE)
    reserved = {s.id for s in reserved_supplies(w, plan_ops, knowledge)}
    free = [s for s in droppable_supplies(w) if s.id not in reserved]
    full = f"pack full ({carry_slots_used(w)}/{w.carry_capacity})"
    if not named:
        return Room(ASK, why=f"{full}: name what to drop for {p.code} with drop; {_choices(free, items)}")
    shed = next((s for s in free if s.code == named), None)
    if shed is None:
        return Room(ASK, why=f"{full}: drop={named} is not held, cannot be dropped, or the plan reserves it; {_choices(free, items)}")
    cost = forfeit(shed.code, items)
    return Room(DROP, shed, f"drop {shed.code or shed.id} for {p.code}" + (f" ({cost})" if cost else ""))


def forfeit(code: str | None, items: dict[str, Any]) -> str:
    """What dropping ``code`` gives up (Manual §11 A price belongs to the
    supply, A158): a priced subtype costs its price again to pick back up,
    wherever it lies. Empty for a subtype no world prices."""
    row = items.get(code) if code else None
    price = row.get("gem_price") if isinstance(row, dict) else None
    if isinstance(price, int) and price > 0:
        return f"forfeits its {price}-gem price"
    sup = supply_row(code)
    return "forfeits its price" if sup is not None and sup.sold else ""


def _choices(free: list[InventorySupply], items: dict[str, Any]) -> str:
    codes = sorted({s.code for s in free if s.code})
    if not codes:
        return "nothing held can be dropped"
    return "droppable: " + ", ".join(f"{c} ({cost})" if (cost := forfeit(c, items)) else c for c in codes)

