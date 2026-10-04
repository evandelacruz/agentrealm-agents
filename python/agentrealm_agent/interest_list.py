"""Interest list nominations for ``Investigate`` (A30)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import Policy
from .curiosity import detour_allowed
from .directives import Directives
from .investigation import cell_was_looked, cell_was_read, spoken_npc_ids, supply_was_read
from .knowledge_base import KnowledgeBase
from .knowledge_maps import door_warp_known, iter_doors
from .memory import Memory
from .world import DOORS, Entity, MapView, Pos, WorldModel, chebyshev
from .zone_discovery import zone_probed

SPEECH_RANGE = 25
SCROLL_CODE_MARK = "scroll"


@dataclass(frozen=True)
class InterestItem:
    kind: Literal["read_block", "read_supply", "say", "walk_look", "zone"]
    reason: str
    free: bool  # nearly free from where we stand (Read/Say in range)
    map_id: int | None = None
    pos: Pos | None = None
    supply_id: int | None = None
    npc: Entity | None = None
    sort_key: tuple = ()


def _brightness_at(w: WorldModel, map_id: int, pos: Pos) -> float:
    fact = w.zones.get(map_id, {}).get(pos)
    return fact.brightness if fact is not None else 1.0


def sight_range(w: WorldModel, map_id: int, at: Pos) -> int:
    """Readable sight: perception × brightness at the stand, capped at perception."""
    bright = _brightness_at(w, map_id, at)
    return max(1, min(w.perception, int(w.perception * bright + 0.999)))


def in_sight(w: WorldModel, map_id: int, at: Pos, target: Pos) -> bool:
    return chebyshev(at, target) <= sight_range(w, map_id, at)


def _is_scroll(code: str) -> bool:
    return SCROLL_CODE_MARK in code.lower()


def investigate_blocked(w: WorldModel, policy: Policy) -> bool:
    """Curiosity pauses while a hostile is within hostile range (M10)."""
    if w.pos is None:
        return True
    here = w.pos
    return any(e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range for e in w.entities)


def _readable_cells(view: MapView) -> list[Pos]:
    return [p for p, flag in view.readable.items() if flag]


def _unread_blocks(w: WorldModel, kb: KnowledgeBase | None, map_id: int, view: MapView, here: Pos) -> list[InterestItem]:
    out: list[InterestItem] = []
    for p in _readable_cells(view):
        if cell_was_read(kb, map_id, p):
            continue
        if not in_sight(w, map_id, here, p):
            continue
        dist = chebyshev(here, p)
        out.append(
            InterestItem(
                "read_block",
                f"read sign @{p[0]},{p[1]}",
                True,
                map_id=map_id,
                pos=p,
                sort_key=(0, dist, p),
            )
        )
    return out


def _scroll_items(w: WorldModel, kb: KnowledgeBase | None, map_id: int, here: Pos) -> list[InterestItem]:
    out: list[InterestItem] = []
    for ent in w.entities:
        if ent.kind != "supply" or not _is_scroll(ent.code):
            continue
        if supply_was_read(kb, ent.id):
            continue
        if not in_sight(w, map_id, here, ent.pos):
            continue
        dist = chebyshev(here, ent.pos)
        out.append(
            InterestItem(
                "read_supply",
                f"read scroll {ent.id}",
                True,
                map_id=map_id,
                pos=ent.pos,
                supply_id=ent.id,
                sort_key=(1, dist, ent.id),
            )
        )
    for sid, _code in w.held_scrolls:
        if supply_was_read(kb, sid):
            continue
        out.append(
            InterestItem(
                "read_supply",
                f"read carried scroll {sid}",
                True,
                map_id=map_id,
                pos=here,
                supply_id=sid,
                sort_key=(1, 0, sid),
            )
        )
    return out


def _say_items(w: WorldModel, kb: KnowledgeBase | None, here: Pos) -> list[InterestItem]:
    spoken = spoken_npc_ids(kb)
    out: list[InterestItem] = []
    for ent in w.entities:
        if ent.kind != "npc" or ent.id in spoken:
            continue
        dist = chebyshev(here, ent.pos)
        if dist > SPEECH_RANGE:
            continue
        out.append(
            InterestItem(
                "say",
                f"say to npc {ent.id}",
                True,
                npc=ent,
                sort_key=(2, dist, ent.id),
            )
        )
    return out


def _door_look_items(w: WorldModel, kb: KnowledgeBase | None, map_id: int, here: Pos, policy: Policy) -> list[InterestItem]:
    if kb is None:
        return []
    out: list[InterestItem] = []
    for door in iter_doors(kb, map_id):
        pos = (int(door["x"]), int(door["y"]))
        if cell_was_looked(kb, map_id, pos):
            continue
        if door_warp_known(door):
            continue
        dist = chebyshev(here, pos)
        out.append(
            InterestItem(
                "walk_look",
                f"look at door @{pos[0]},{pos[1]}",
                False,
                map_id=map_id,
                pos=pos,
                sort_key=(4, dist, pos),
            )
        )
    for key, _meta in _entrances(kb):
        try:
            x, y = (int(p) for p in key.split(",", 1))
        except ValueError:
            continue
        pos = (x, y)
        if cell_was_looked(kb, map_id, pos):
            continue
        dist = chebyshev(here, pos)
        out.append(
            InterestItem(
                "walk_look",
                f"look at entrance @{pos[0]},{pos[1]}",
                False,
                map_id=map_id,
                pos=pos,
                sort_key=(4, dist, pos),
            )
        )
    return out


def _entrances(kb: KnowledgeBase) -> list[tuple[str, dict]]:
    with kb.lock:
        raw = kb.entrances
        return list(raw.items()) if isinstance(raw, dict) else []


def _zone_items(w: WorldModel, kb: KnowledgeBase | None, map_id: int, here: Pos, path: list[Pos]) -> list[InterestItem]:
    """Zone reads on the current plan path (A7 keeps respawn/path probes)."""
    if not path:
        return []
    candidates: set[Pos] = set(path)
    out: list[InterestItem] = []
    for p in sorted(candidates):
        if zone_probed(w, map_id, p):
            continue
        dist = chebyshev(here, p)
        out.append(
            InterestItem(
                "zone",
                f"get_zone @{p[0]},{p[1]}",
                True,
                map_id=map_id,
                pos=p,
                sort_key=(3, dist, p),
            )
        )
    return out


def list_interest(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy: Policy,
    m: Memory,
    directives: Directives,
) -> list[InterestItem]:
    """Everything the knowledge base has not finished investigating."""
    if w.pos is None or w.map_id is None or investigate_blocked(w, policy):
        return []
    here = w.pos
    map_id = w.map_id
    view = w.view
    items: list[InterestItem] = []
    items.extend(_unread_blocks(w, kb, map_id, view, here))
    items.extend(_scroll_items(w, kb, map_id, here))
    items.extend(_say_items(w, kb, here))
    items.extend(_zone_items(w, kb, map_id, here, m.path))
    allow_detour = detour_allowed(m, w.tick, float(directives.params.get("curiosity", 0.2)))
    if allow_detour:
        items.extend(_door_look_items(w, kb, map_id, here, policy))
    items.sort(key=lambda it: it.sort_key)
    return items


def pick_interest(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy: Policy,
    m: Memory,
    directives: Directives,
) -> InterestItem | None:
    items = list_interest(w, kb, policy, m, directives)
    if not items:
        return None
    allow_detour = detour_allowed(m, w.tick, float(directives.params.get("curiosity", 0.2)))
    for it in items:
        if it.free or allow_detour:
            return it
    return None


def pick_interest_tick(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy: Policy,
    m: Memory,
    directives: Directives,
) -> InterestItem | None:
    """Top interest item that becomes a tick intent (not ``get_zone``)."""
    allow_detour = detour_allowed(m, w.tick, float(directives.params.get("curiosity", 0.2)))
    for it in list_interest(w, kb, policy, m, directives):
        if it.kind == "zone":
            continue
        if it.free or allow_detour:
            return it
    return None


def next_interest_zone_probe(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy: Policy,
    m: Memory,
    directives: Directives,
) -> tuple[int, Pos] | None:
    """Zone read for the top interest-list zone item, if any."""
    if investigate_blocked(w, policy):
        return None
    for it in list_interest(w, kb, policy, m, directives):
        if it.kind == "zone" and it.map_id is not None and it.pos is not None:
            return it.map_id, it.pos
    return None


def walk_target_for_look(w: WorldModel, door_pos: Pos, avoid: set[Pos]) -> Pos | None:
    """Stand next to a door or mark; being on a door tile is fine too."""
    if w.pos is None:
        return None
    if chebyshev(w.pos, door_pos) <= 1:
        return None
    if w.view.tiles.get(door_pos) in DOORS:
        if w.view.walkable(door_pos) and door_pos not in w.occupied() and door_pos not in avoid:
            return door_pos
    best: Pos | None = None
    best_dist = 10**9
    for n in w.neighbours(door_pos):
        if not w.view.walkable(n) or n in avoid or n in w.occupied():
            continue
        d = chebyshev(w.pos, n)
        if d < best_dist:
            best_dist, best = d, n
    return best
