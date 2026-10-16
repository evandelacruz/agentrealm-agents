"""Safe-tile discovery via ``get_zone`` and terrain reads (A7).

Probes cells around respawn anchors and along the current path when the
scheduler would otherwise skip a calm window, so later survival states
have known safe tiles without spending urgent budget on zone reads. While
Travel searches for a hunting ground, its probes come first (``hunt_probe``). A
terrain read already marks safe-zone cells (``MapView.safe``), so those
are known safe and never probed. The tradeoff: an agent's terrain read
leaves out ``brightness``, so such a cell's brightness stays unread and
counts as 1 (``investigation.sight_range``), which overstates sight in a dim
safe zone. ``safe_zone_of`` is the safe zone a cell lies in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .world import NEIGHBOURS, Pos, ZoneFact, chebyshev

if TYPE_CHECKING:
    from .memory import Memory
    from .world import WorldModel

# Chebyshev radius around each respawn anchor for the first pass of probes.
RESPAWN_PROBE_RADIUS = 8
# While Travel searches for a hunting ground (A27), spare windows read one
# cell in every HUNT_PROBE_SPACING × HUNT_PROBE_SPACING square, in widening
# rings around the character out to HUNT_PROBE_RADIUS, so a search covers
# ground without reading every cell. One search reads at most
# HUNT_PROBE_BUDGET cells.
HUNT_PROBE_SPACING = 4
HUNT_PROBE_RADIUS = 64
HUNT_PROBE_BUDGET = 200


def apply_town(w: WorldModel, town: dict | None) -> None:
    if not town:
        return
    try:
        w.record_respawn_anchor(int(town["map_id"]), (int(town["x"]), int(town["y"])))
    except (KeyError, TypeError, ValueError):
        return


def zone_probed(w: WorldModel, map_id: int, pos: Pos) -> bool:
    """Read already, the read failed, or a terrain read showed it safe: not probed again."""
    if pos in w.zones.get(map_id, {}) or (map_id, pos) in w.zone_failed:
        return True
    view = w.maps.get(map_id)
    return view is not None and pos in view.safe


def apply_zone(w: WorldModel, map_id: int, x: int, y: int, body: dict) -> ZoneFact:
    """Records a ``get_zone`` answer for a cell."""
    fact = ZoneFact(
        safe=bool(body.get("safe")),
        brightness=float(body.get("brightness", 1)),
        strength_ceiling=_opt_int(body.get("strength_ceiling")),
    )
    w.zones.setdefault(map_id, {})[(x, y)] = fact
    return fact


def zone_failed(w: WorldModel, map_id: int, pos: Pos) -> None:
    """Records a failed ``get_zone`` (e.g. unrevealed cell) so it is not re-probed."""
    w.zone_failed.add((map_id, pos))


def safe_tiles(w: WorldModel, map_id: int) -> set[Pos]:
    """Known safe cells on a map, from zone and terrain reads (PLAN.md A7).
    **Heal** (A10) walks to them; Recover reads it to pick a safe tile beside
    the death chest (A11); Retreat will read it too (A9)."""
    view = w.maps.get(map_id)
    from_terrain = set(view.safe) if view is not None else set()
    return from_terrain | {pos for pos, fact in w.zones.get(map_id, {}).items() if fact.safe}


def safe_zone_of(w: WorldModel, map_id: int, pos: Pos, safe: set[Pos] | None = None) -> set[Pos]:
    """The known safe cells joined (8-way) to ``pos``: one safe zone as far as
    it is known. Empty when ``pos`` is not known safe. ``safe`` is
    ``safe_tiles`` when the caller already has it."""
    safe = safe_tiles(w, map_id) if safe is None else safe
    if pos not in safe:
        return set()
    zone, todo = {pos}, [pos]
    while todo:
        x, y = todo.pop()
        for dx, dy in NEIGHBOURS:
            n = (x + dx, y + dy)
            if n in safe and n not in zone:
                zone.add(n)
                todo.append(n)
    return zone


def known_safe(w: WorldModel, map_id: int, pos: Pos) -> bool:
    """A zone or terrain read showed ``pos`` inside a safe zone (town, a respawn patch)."""
    view = w.maps.get(map_id)
    if view is not None and pos in view.safe:
        return True
    fact = w.zones.get(map_id, {}).get(pos)
    return fact is not None and fact.safe


def next_zone_probe(w: WorldModel, m: Memory) -> tuple[int, Pos] | None:
    """The next revealed cell that still needs a zone read, or None.

    While Travel searches for a hunting ground, its probe comes first
    (``hunt_probe``), counted against the search's budget. Otherwise cells
    within RESPAWN_PROBE_RADIUS of a respawn anchor, nearest the anchor; then
    cells on the current path. Only revealed cells qualify, since get_zone
    refuses an unrevealed one. Called once per spare window by choose_call,
    which hands the pick to the runner in Memory.zone_probe.

    The search goes first because the respawn ring alone can fill every spare
    window: with an empty knowledge base it took all 211 zone reads of a
    five-minute search, and no hunt probe ever ran (A23 survive-a-fight run 2).
    """
    if w.pos is None:
        return None
    probe = hunt_probe(w, m)
    if probe is not None and w.map_id is not None and m.hunt_search is not None:
        m.hunt_search.probes += 1
        return w.map_id, probe
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
        if key in seen or not needs_zone_read(w, map_id, pos):
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
        # The ring around the anchor, not the whole map: bounded per window.
        for dy in range(-RESPAWN_PROBE_RADIUS, RESPAWN_PROBE_RADIUS + 1):
            for dx in range(-RESPAWN_PROBE_RADIUS, RESPAWN_PROBE_RADIUS + 1):
                add(map_id, (anchor[0] + dx, anchor[1] + dy), 0)

    if w.map_id is not None:
        for step in m.path:
            add(w.map_id, step, 1)

    if not candidates:
        return None
    candidates.sort()
    _pri, _dist, map_id, pos = candidates[0]
    return map_id, pos


def needs_zone_read(w: WorldModel, map_id: int, pos: Pos) -> bool:
    """Revealed (get_zone refuses an unrevealed cell) and not read yet."""
    view = w.maps.get(map_id)
    return view is not None and pos in view.tiles and not zone_probed(w, map_id, pos)


def hunt_probe(w: WorldModel, m: Memory) -> Pos | None:
    """The next cell to read while Travel searches for a hunting ground (A27),
    or None: ``get_zone`` gives ``strength_ceiling`` only on a hunting cell.

    The search ranges outward from the character. It walks the map's
    frontier, so the cells around it are the newest ground. It reads one cell
    in every ``HUNT_PROBE_SPACING`` square, in widening rings of those grid
    cells around the character, out to ``HUNT_PROBE_RADIUS``. Within a ring
    the nearest cell comes first. A cell read already, or unrevealed, is
    skipped.

    None when no search is on, none is fresh (``HuntSearch.probe_until``),
    or the search has spent its ``HUNT_PROBE_BUDGET`` reads. A read that
    finds a hunting cell Travel may enter ends the search
    (``states/travel.py``), and with it the probes.
    """
    s = m.hunt_search
    if s is None or w.pos is None or w.map_id is None or w.tick > s.probe_until:
        return None
    if s.probes >= HUNT_PROBE_BUDGET:
        return None
    cx, cy = w.pos
    # The grid cell nearest us: grid cells sit on multiples of the spacing.
    gx = round(cx / HUNT_PROBE_SPACING) * HUNT_PROBE_SPACING
    gy = round(cy / HUNT_PROBE_SPACING) * HUNT_PROBE_SPACING
    for ring in range(HUNT_PROBE_RADIUS // HUNT_PROBE_SPACING + 1):
        cells = [
            (gx + i * HUNT_PROBE_SPACING, gy + j * HUNT_PROBE_SPACING)
            for j in range(-ring, ring + 1)
            for i in range(-ring, ring + 1)
            if max(abs(i), abs(j)) == ring
        ]
        open_cells = [p for p in cells if needs_zone_read(w, w.map_id, p)]
        if open_cells:
            return min(open_cells, key=lambda p: (chebyshev(p, w.pos), p))
    return None


def _opt_int(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
