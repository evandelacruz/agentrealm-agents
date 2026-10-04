"""What each Step rejection teaches the navigation map (A14).

See docs/PLAYABLE_AGENT_PLAN.md Navigation §2. Every learning is keyed by
(map_id, cell), so a lesson from one map never blocks the same coordinates
on another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..knowledge_maps import iter_doors, record_hunting_zone, record_locked_door
from ..world import DOORS, Pos

if TYPE_CHECKING:
    from ..knowledge_base import KnowledgeBase
    from ..world import WorldModel

MapPos = tuple[int, Pos]

# Occupant cost lasts a few seconds at 10 ticks/s (PLAYABLE_AGENT_PLAN §2).
OCCUPANT_LEARN_TICKS = 30


@dataclass
class NavMemory:
    """Per-character navigation learnings from rejections."""

    impassable: set[MapPos] = field(default_factory=set)
    occupant_until: dict[MapPos, int] = field(default_factory=dict)
    wait_tile: MapPos | None = None  # block_occupied or unhandled code: keep off for one decision


def copy_nav(nav: NavMemory) -> NavMemory:
    return NavMemory(
        impassable=set(nav.impassable),
        occupant_until=dict(nav.occupant_until),
        wait_tile=nav.wait_tile,
    )


def locked_doors_from_kb(kb: KnowledgeBase | None, map_id: int) -> set[Pos]:
    if kb is None:
        return set()
    out: set[Pos] = set()
    for door in iter_doors(kb, map_id):
        if door.get("locked"):
            try:
                out.add((int(door["x"]), int(door["y"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def navigation_avoid_costly(
    nav: NavMemory,
    kb: KnowledgeBase | None,
    map_id: int,
    tick: int,
) -> tuple[set[Pos], set[Pos]]:
    """Cells on map_id to treat as impassable or extra-cost. Read-only."""
    avoid = {p for mid, p in nav.impassable if mid == map_id} | locked_doors_from_kb(kb, map_id)
    if nav.wait_tile is not None and nav.wait_tile[0] == map_id:
        avoid.add(nav.wait_tile[1])
    costly = {p for (mid, p), until in nav.occupant_until.items() if mid == map_id and until > tick}
    return avoid, costly


def end_decision(nav: NavMemory, tick: int) -> None:
    """Ages the learnings once per decision: the one place they are pruned."""
    nav.wait_tile = None
    nav.occupant_until = {k: until for k, until in nav.occupant_until.items() if until > tick}


def record_strength_closed(kb: KnowledgeBase | None, w: WorldModel, pos: Pos) -> int | None:
    """Mark a hunting cell closed in the knowledge base. Returns its ceiling
    when a ``get_zone`` read has served it, for A27's strength bracket."""
    if w.map_id is None:
        return None
    fact = w.zones.get(w.map_id, {}).get(pos)
    ceiling = fact.strength_ceiling if fact is not None else None
    record_hunting_zone(kb, w.map_id, pos, ceiling, closed=True)
    return ceiling


def learn_step_rejection(
    m,
    w: WorldModel,
    kb: KnowledgeBase | None,
    landing: Pos,
    code: str | None,
    tick: int,
) -> None:
    """Reflex 1 plus A14: drop the plan and teach the map from the code."""
    m.path, m.goal = [], ""
    m.corridors.clear()  # the corridor searches priced the map before this lesson (A13)
    nav = m.nav
    cell = (w.map_id, landing)
    match code:
        case "not_traversable":
            nav.impassable.add(cell)
        case "block_occupied":
            nav.wait_tile = cell
            nav.occupant_until[cell] = tick + OCCUPANT_LEARN_TICKS
        case "conflict_lost":
            pass
        case "door_locked":
            nav.impassable.add(cell)
            if kb is not None:
                block = w.view.tiles.get(landing)
                record_locked_door(kb, w.map_id, landing, block if block in DOORS else "framed_door")
        case "over_strength_ceiling":
            nav.impassable.add(cell)
            # A loadout change reopens the cell (A27: StrengthBracket.reset).
            m.strength.note_over(cell, record_strength_closed(kb, w, landing))
        case _:
            # A code this table does not handle (or none): keep off the cell for
            # one decision, as reflex 1 did before A14, and learn nothing more.
            # would_strand lands here by design (PLAN.md A14): the server refuses
            # the move that would strand us, so there is no landing to plan.
            nav.wait_tile = cell


def on_block_changed(m, map_id: int, pos: Pos) -> None:
    """A changed block may be passable again: forget the learned block."""
    m.nav.impassable.discard((map_id, pos))
