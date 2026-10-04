"""Fragment helpers for Compose (A39)."""

from __future__ import annotations

from typing import Iterable

from .item_table import FragmentMeta, InventorySupply


def fragments_for(held: Iterable[InventorySupply], composes_into: str) -> list[tuple[InventorySupply, FragmentMeta]]:
    return [(s, s.fragment) for s in held if s.fragment is not None and s.fragment.composes_into == composes_into]


def holds_whole(held: Iterable[InventorySupply], composes_into: str) -> bool:
    """True when a finished supply for ``composes_into`` is carried (not a fragment)."""
    return any(s.code == composes_into and s.fragment is None for s in held)


def one_per_slot(held: Iterable[InventorySupply], composes_into: str) -> dict[int, InventorySupply]:
    """The lowest-id held fragment for each slot of ``composes_into``."""
    out: dict[int, InventorySupply] = {}
    for s, meta in sorted(fragments_for(held, composes_into), key=lambda f: f[0].id):
        out.setdefault(meta.slot, s)
    return out


def fragment_set_complete(held: Iterable[InventorySupply], composes_into: str) -> bool:
    """All pieces for ``composes_into`` are held and ready to Compose.

    A held fragment whose ``missing_slots`` names a piece means the set is not
    complete; otherwise every slot up to ``piece_count`` must be held.
    """
    held = list(held)
    if holds_whole(held, composes_into):
        return True
    frags = fragments_for(held, composes_into)
    if not frags:
        return False
    if any(meta.missing_slots for _, meta in frags):
        return False
    piece_count = max(meta.piece_count for _, meta in frags)
    return len(one_per_slot(held, composes_into)) >= piece_count


def compose_supply_ids(held: Iterable[InventorySupply], composes_into: str) -> list[int]:
    """One supply id per slot, in slot order; a duplicate piece stays in the inventory."""
    by_slot = one_per_slot(held, composes_into)
    return [by_slot[slot].id for slot in sorted(by_slot)]
