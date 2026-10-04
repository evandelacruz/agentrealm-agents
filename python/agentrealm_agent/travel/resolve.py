"""Resolve a travel op to a map and cell (A27)."""

from __future__ import annotations

from dataclasses import dataclass

from ..knowledge_base import KnowledgeBase
from ..world import Pos, WorldModel, chebyshev
from .knowledge import entrance_from_kb, iter_hunting_cells, iter_shop_cells, town_from_kb
from .ops import TravelOp
from .strength import StrengthBracket


@dataclass(frozen=True)
class ResolvedDestination:
    map_id: int
    pos: Pos
    label: str


def resolve_travel(
    op: TravelOp,
    w: WorldModel,
    kb: KnowledgeBase | None,
    bracket: StrengthBracket,
) -> ResolvedDestination | None:
    if op.to == "town":
        town = town_from_kb(kb)
        if town is None and w.respawn_anchors:
            mid, pos = w.respawn_anchors[0]
            return ResolvedDestination(mid, pos, "town")
        if town is not None:
            return ResolvedDestination(town[0], town[1], "town")
        return None
    if op.to == "point":
        if op.x is None or op.y is None:
            return None
        dest_map = op.map_id if op.map_id is not None else w.map_id
        if dest_map is None:
            return None
        return ResolvedDestination(dest_map, (op.x, op.y), "point")
    if op.to == "entrance":
        if op.x is None or op.y is None:
            return None
        resolved = entrance_from_kb(kb, op.map_id, op.x, op.y)
        if resolved is None:
            return None
        return ResolvedDestination(resolved[0], resolved[1], "entrance")
    if op.to == "shop":
        if op.x is not None and op.y is not None:
            dest_map = op.map_id if op.map_id is not None else w.map_id
            if dest_map is None:
                return None
            return ResolvedDestination(dest_map, (op.x, op.y), "shop")
        shops = iter_shop_cells(kb)
        if not shops:
            return None
        here = w.pos
        if here is None or w.map_id is None:
            mid, pos = shops[0]
            return ResolvedDestination(mid, pos, "shop")
        best = min(shops, key=lambda s: (0 if s[0] == w.map_id else 1, chebyshev(here, s[1]), s))
        return ResolvedDestination(best[0], best[1], "shop")
    if op.to == "hunting_ground":
        return _resolve_hunting(w, kb, bracket)
    return None


def _resolve_hunting(
    w: WorldModel,
    kb: KnowledgeBase | None,
    bracket: StrengthBracket,
) -> ResolvedDestination | None:
    candidates: list[tuple[int, Pos, int | None]] = []
    for map_id, pos, fact in iter_hunting_cells(kb):
        ceiling = fact.get("strength_ceiling")
        c = int(ceiling) if isinstance(ceiling, int) and not isinstance(ceiling, bool) else None
        if not bracket.can_enter_ceiling(c):
            continue
        candidates.append((map_id, pos, c))
    if w.map_id is not None and w.pos is not None:
        for pos, fact in w.zones.get(w.map_id, {}).items():
            if fact.strength_ceiling is None:
                continue
            if not bracket.can_enter_ceiling(fact.strength_ceiling):
                continue
            candidates.append((w.map_id, pos, fact.strength_ceiling))
    if not candidates:
        return None
    best_ceiling = max((c for _, _, c in candidates if c is not None), default=None)
    tier = [t for t in candidates if t[2] == best_ceiling or (best_ceiling is None and t[2] is None)]
    here = w.pos
    if here is None:
        pick = tier[0]
    else:
        pick = min(tier, key=lambda t: (0 if t[0] == w.map_id else 1, chebyshev(here, t[1]), t))
    return ResolvedDestination(pick[0], pick[1], "hunting_ground")


def at_destination(w: WorldModel, dest: ResolvedDestination) -> bool:
    return w.map_id == dest.map_id and w.pos == dest.pos
