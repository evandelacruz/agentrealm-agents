"""What each Step rejection teaches the navigation map (A14).

See docs/PLAYABLE_AGENT_PLAN.md Navigation §2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..world import Pos

if TYPE_CHECKING:
    from ..knowledge_base import KnowledgeBase
    from ..world import WorldModel

# Occupant cost lasts a few seconds at 10 ticks/s (PLAYABLE_AGENT_PLAN §2).
OCCUPANT_LEARN_TICKS = 30


@dataclass
class NavMemory:
    """Per-character navigation learnings from rejections."""

    impassable: set[Pos] = field(default_factory=set)
    occupant_until: dict[Pos, int] = field(default_factory=dict)
    wait_tile: Pos | None = None  # block_occupied: keep off for one decision
    prefer_land: Pos | None = None  # would_strand: step here before other goals


def copy_nav(nav: NavMemory) -> NavMemory:
    return NavMemory(
        impassable=set(nav.impassable),
        occupant_until=dict(nav.occupant_until),
        wait_tile=nav.wait_tile,
        prefer_land=nav.prefer_land,
    )


def locked_doors_from_kb(kb: KnowledgeBase | None, map_id: int) -> set[Pos]:
    if kb is None:
        return set()
    section = kb.maps.get(str(map_id)) or {}
    out: set[Pos] = set()
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
    """Tiles to treat as impassable or extra-cost for the cost grid."""
    nav.occupant_until = {p: until for p, until in nav.occupant_until.items() if until > tick}
    avoid = set(nav.impassable) | locked_doors_from_kb(kb, map_id)
    if nav.wait_tile is not None:
        avoid.add(nav.wait_tile)
    costly = set(nav.occupant_until)
    return avoid, costly


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
    match code:
        case "not_traversable":
            nav.impassable.add(landing)
        case "block_occupied":
            nav.wait_tile = landing
            nav.occupant_until[landing] = tick + OCCUPANT_LEARN_TICKS
        case "conflict_lost":
            pass
        case "door_locked":
            nav.impassable.add(landing)
            if kb is not None:
                record_locked_door(kb, w.map_id, landing[0], landing[1])
        case "over_strength_ceiling":
            nav.impassable.add(landing)
            record_strength_closed(kb, w, landing)
        case "would_strand":
            nav.prefer_land = landing
        case _:
            nav.wait_tile = landing


def on_block_changed(m, map_id: int, pos: Pos, our_map_id: int) -> None:
    if map_id == our_map_id:
        m.nav.impassable.discard(pos)


def consume_wait_tile(m) -> None:
    """The one-decision keep-off from block_occupied (or unknown codes)."""
    m.nav.wait_tile = None


def clear_prefer_land_on_step(m, landed: Pos) -> None:
    if m.nav.prefer_land == landed:
        m.nav.prefer_land = None
