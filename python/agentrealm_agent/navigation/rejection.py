"""What each Step rejection teaches the navigation map (A14).

See docs/PLAYABLE_AGENT_PLAN.md Navigation §2. Every learning is keyed by
(map_id, cell), so a lesson from one map never blocks the same coordinates
on another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..world import Pos

if TYPE_CHECKING:
    from ..knowledge_base import KnowledgeBase
    from ..world import WorldModel

MapPos = tuple[int, Pos]

# Occupant cost lasts a few seconds at 10 ticks/s (PLAYABLE_AGENT_PLAN §2).
OCCUPANT_LEARN_TICKS = 30
# Decisions the would_strand land step is tried before it is dropped.
LAND_TRIES = 3


@dataclass
class NavMemory:
    """Per-character navigation learnings from rejections."""

    impassable: set[MapPos] = field(default_factory=set)
    occupant_until: dict[MapPos, int] = field(default_factory=dict)
    wait_tile: MapPos | None = None  # block_occupied: keep off for one decision
    prefer_land: MapPos | None = None  # would_strand: step here before other goals
    land_tries: int = 0  # land steps sent for prefer_land


def copy_nav(nav: NavMemory) -> NavMemory:
    return NavMemory(
        impassable=set(nav.impassable),
        occupant_until=dict(nav.occupant_until),
        wait_tile=nav.wait_tile,
        prefer_land=nav.prefer_land,
        land_tries=nav.land_tries,
    )


def locked_doors_from_kb(kb: KnowledgeBase | None, map_id: int) -> set[Pos]:
    if kb is None:
        return set()
    out: set[Pos] = set()
    with kb.lock:
        section = kb.maps.get(str(map_id)) or {}
        for door in section.get("doors") or []:
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


def record_locked_door(kb: KnowledgeBase, map_id: int, x: int, y: int) -> None:
    with kb.lock:
        section = kb.maps.setdefault(str(map_id), {})
        doors = section.setdefault("doors", [])
        for door in doors:
            if door.get("x") == x and door.get("y") == y:
                door["locked"] = True
                return
        doors.append({"x": x, "y": y, "locked": True})


def record_strength_closed(kb: KnowledgeBase | None, w: WorldModel, pos: Pos) -> None:
    """Stores the closure for A27's strength bracket. Navigation keeps off the
    cell through NavMemory.impassable; nothing reads this entry yet."""
    if kb is None:
        return
    fact = w.zones.get(w.map_id, {}).get(pos)
    entry: dict = {"closed": True}
    if fact is not None and fact.strength_ceiling is not None:
        entry["strength_ceiling"] = fact.strength_ceiling
    with kb.lock:
        hunting = kb.maps.setdefault(str(w.map_id), {}).setdefault("hunting", {})
        hunting[f"{pos[0]},{pos[1]}"] = entry


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
    nav = m.nav
    cell = (w.map_id, landing)
    if nav.prefer_land == cell:
        # The land step itself was refused: drop it rather than retry forever.
        nav.prefer_land, nav.land_tries = None, 0
        if code == "would_strand":
            nav.wait_tile = cell
            return
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
                record_locked_door(kb, w.map_id, landing[0], landing[1])
        case "over_strength_ceiling":
            nav.impassable.add(cell)
            record_strength_closed(kb, w, landing)
        case "would_strand":
            nav.prefer_land, nav.land_tries = cell, 0
        case _:
            # A code this table does not know (or none): keep off the cell for
            # one decision, as reflex 1 did before A14, and learn nothing more.
            nav.wait_tile = cell


def on_block_changed(m, map_id: int, pos: Pos) -> None:
    """A changed block may be passable again: forget the learned block."""
    m.nav.impassable.discard((map_id, pos))


def clear_prefer_land_on_step(m, map_id: int, landed: Pos) -> None:
    if m.nav.prefer_land == (map_id, landed):
        m.nav.prefer_land, m.nav.land_tries = None, 0
