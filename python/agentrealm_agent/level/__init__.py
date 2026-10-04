"""Level context: whether the character is inside a level (A37)."""

from __future__ import annotations

from ..knowledge_base import KnowledgeBase
from ..travel.knowledge import town_from_kb
from ..world import WorldModel


def overworld_map_id(kb: KnowledgeBase | None) -> int | None:
    """The overworld map (town map) when the knowledge base has synced town."""
    town = town_from_kb(kb)
    return town[0] if town is not None else None


def inside_level(w: WorldModel, _kb: KnowledgeBase | None = None) -> bool:
    """True when the position read names a positive ``level`` (Manual §5.3).

    Interior maps carry ``level``; the overworld omits it or sends zero. The
    knowledge base town map (A41) is exported for metrics and other callers,
    not for this guard.
    """
    if not w.alive or w.map_id is None:
        return False
    return w.map_level is not None and w.map_level > 0
