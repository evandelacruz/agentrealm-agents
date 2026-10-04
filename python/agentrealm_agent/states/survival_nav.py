"""Shared navigation setup for survival states (A9)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation.rejection import navigation_avoid_costly
from ..world import Pos, WorldModel


def plan_surfaces(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
) -> tuple[set[Pos], set[Pos], set[Pos], set[Pos]]:
    """Returns blocked, escape, plan_avoid, plan_costly for pathing."""
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    here = w.pos
    escape: set[Pos] = set()
    if here is not None and here in hazards:
        if w.open_neighbours(here, blocked):
            escape = set()
        else:
            escape = hazards
    plan_avoid = blocked - escape
    plan_costly = escape | nav_costly
    return blocked, escape, plan_avoid, plan_costly
