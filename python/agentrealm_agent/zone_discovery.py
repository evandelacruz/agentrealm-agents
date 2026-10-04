"""Safe-tile discovery via ``get_zone`` (A7).

Probes cells around respawn anchors and along the current path when the
scheduler would otherwise skip a calm window, so later survival states
have known safe tiles without spending urgent budget on zone reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .world import Pos, chebyshev

if TYPE_CHECKING:
    from .brain import Memory
    from .world import WorldModel

# Chebyshev radius around each respawn anchor for the first pass of probes.
RESPAWN_PROBE_RADIUS = 8


@dataclass(frozen=True)
class ZoneFact:
    safe: bool
    brightness: float = 1.0
    strength_ceiling: int | None = None


def record_respawn_anchor(w: WorldModel, map_id: int, pos: Pos) -> None:
    anchor = (map_id, pos)
    if anchor not in w.respawn_anchors:
        w.respawn_anchors.append(anchor)


def apply_town(w: WorldModel, town: dict | None) -> None:
    if not town:
        return
    record_respawn_anchor(w, int(town["map_id"]), (int(town["x"]), int(town["y"])))


def zone_probed(w: WorldModel, map_id: int, pos: Pos) -> bool:
    return pos in w.zones.get(map_id, {})


def apply_zone(w: WorldModel, map_id: int, x: int, y: int, body: dict) -> ZoneFact:
    """Records a ``get_zone`` answer for a cell."""
    pos = (x, y)
    fact = ZoneFact(
        safe=bool(body.get("safe")),
        brightness=float(body.get("brightness", 1)),
        strength_ceiling=_opt_int(body.get("strength_ceiling")),
    )
    w.zones.setdefault(map_id, {})[pos] = fact
    if fact.safe:
        w.safe_tiles.setdefault(map_id, set()).add(pos)
    return fact


def safe_tiles(w: WorldModel, map_id: int | None = None) -> set[Pos]:
    if map_id is None:
        out: set[Pos] = set()
        for tiles in w.safe_tiles.values():
            out |= tiles
        return out
    return set(w.safe_tiles.get(map_id, ()))


def nearest_known_safe(w: WorldModel) -> tuple[Pos, list[Pos]] | None:
    """Nearest safe tile on the current map by path, with its path."""
    if w.map_id is None or w.pos is None:
        return None
    targets = safe_tiles(w, w.map_id)
    if not targets:
        return None
    found = w.nearest(targets)
    return found


def next_zone_probe(w: WorldModel, m: Memory) -> tuple[int, Pos] | None:
    """The next revealed cell that still needs a zone read, or None."""
    if w.pos is None:
        return None
    here = w.pos
    candidates: list[tuple[int, int, int, Pos]] = []  # priority, sort dist, map_id, pos
    seen: set[tuple[int, Pos]] = set()

    def anchor_distance(map_id: int, pos: Pos) -> int:
        dist = 10**6
        for mid, anchor in w.respawn_anchors:
            if mid == map_id:
                dist = min(dist, chebyshev(pos, anchor))
        return dist

    def add(map_id: int, pos: Pos, priority: int) -> None:
        key = (map_id, pos)
        if key in seen or zone_probed(w, map_id, pos):
            return
        view = w.maps.get(map_id)
        if view is None or pos not in view.tiles:
            return
        seen.add(key)
        if priority == 0:
            sort_dist = anchor_distance(map_id, pos)
        elif w.map_id == map_id:
            sort_dist = chebyshev(here, pos)
        else:
            sort_dist = 10**6
        candidates.append((priority, sort_dist, map_id, pos))

    for map_id, anchor in w.respawn_anchors:
        view = w.maps.get(map_id)
        if view is None:
            add(map_id, anchor, 0)
            continue
        for pos, _block in view.tiles.items():
            if chebyshev(pos, anchor) <= RESPAWN_PROBE_RADIUS:
                add(map_id, pos, 0)

    for step in m.path:
        if w.map_id is not None:
            add(w.map_id, step, 1)

    if not candidates:
        return None
    candidates.sort()
    _pri, _dist, map_id, pos = candidates[0]
    return map_id, pos


def _opt_int(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
