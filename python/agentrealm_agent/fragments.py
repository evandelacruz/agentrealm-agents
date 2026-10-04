"""Fragment helpers for Compose (A39)."""

from __future__ import annotations

from typing import Iterable

from .item_table import InventorySupply


def fragments_for(held: Iterable[InventorySupply], composes_into: str) -> list[InventorySupply]:
    return [s for s in held if s.fragment is not None and s.fragment.composes_into == composes_into]


def holds_whole(held: Iterable[InventorySupply], composes_into: str) -> bool:
    """True when a finished supply for ``composes_into`` is carried (not a fragment)."""
    return any(s.code == composes_into and s.fragment is None for s in held)


def fragment_set_complete(held: Iterable[InventorySupply], composes_into: str) -> bool:
    """All pieces for ``composes_into`` are held and ready to Compose."""
    if holds_whole(held, composes_into):
        return True
    frags = fragments_for(held, composes_into)
    if not frags:
        return False
    meta = frags[0].fragment
    assert meta is not None
    if meta.missing_slots:
        return False
    slots = {s.fragment.slot for s in frags if s.fragment is not None}
    return len(slots) >= meta.piece_count


def compose_supply_ids(held: Iterable[InventorySupply], composes_into: str) -> list[int]:
    return [s.id for s in fragments_for(held, composes_into)]
