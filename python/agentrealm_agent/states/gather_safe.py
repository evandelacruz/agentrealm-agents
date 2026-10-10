"""Which ground **Gather** may work, and which counts as safe-ish (A22). Not a ``STATES`` entry.

``GatherState`` targets a gem pile, grass or bush only where ``gather_ground``
holds: known ground off hazards with no known hostile near, in view or
remembered (``known_reach``), and walks there only by a route clear of every
known hostile's reach (``route_clear``). Detour (A71) uses the same two
tests for its finds. That includes the
open field, where the gems are, and safe zones, where breaking a block is
allowed; Gather prefers field cells and learns which ground does not cut
(``gem_yield``). Staying alive in the field is the reflexes' job (Retreat,
Flee, Fight, Heal), not Gather's. ``is_safe_ish`` is the stricter safe-zone
ground a hurt character walks first (``safe_explore_path``).
"""

from __future__ import annotations

from ..config import Policy
from ..navigation.planner import HOSTILE_DANGER_RADIUS
from ..survival import hostile_reach, is_attacker, is_hostile, recently_attacked
from ..world import NEIGHBOURS, Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import RESPAWN_PROBE_RADIUS, safe_tiles


# Gather keeps this far from the hostile that hit us: the radius inside which
# the path planner already prices a hostile's cells as dangerous.
GATHER_HOSTILE_RADIUS = HOSTILE_DANGER_RADIUS
# A hostile that has not hit us bars only cells within our weapon reach of it,
# or ``policy.hostile_range`` when that is further, plus this many steps: one
# that followed at 4–6 blocks without attacking stopped all cutting for 45 s
# under the full radius (A63 run 3).
GATHER_SHADOW_MARGIN = 1


def gather_bar(w: WorldModel, e: Entity, policy: Policy) -> int:
    """How far from threat ``e`` (``is_hostile``) Gather keeps the cells it works.

    Never inside ``policy.hostile_range``, where Retreat, Flee and Fight
    start, and a step clear of it: a bar shorter than that range sent Gather
    to a cell Retreat walked straight back from, 4 times in 34 s (A63 run 4).
    """
    if recently_attacked(w) and is_attacker(w, e):
        return max(GATHER_HOSTILE_RADIUS, policy.hostile_range + GATHER_SHADOW_MARGIN)
    return max(w.attack_range or 1, policy.hostile_range) + GATHER_SHADOW_MARGIN


def hostiles_near(w: WorldModel, pos: Pos, policy: Policy, radius: int | None = None) -> bool:
    """A threat (``is_hostile``) in view within ``radius`` of ``pos``
    (``policy.hostile_range`` by default). Townsfolk are not one."""
    reach = policy.hostile_range if radius is None else radius
    return any(is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= reach for e in w.entities)


def gather_ground(w: WorldModel, pos: Pos, policy: Policy, *, fight: bool = False) -> bool:
    """Known ground where cutting grass or a bush, or taking a gem pile, is allowed.

    The cell must be revealed, not on a hazard tile, and have no known
    hostile in view within its ``gather_bar``: ``GATHER_HOSTILE_RADIUS`` for
    the one that hit us, a step past our weapon reach or ``policy.hostile_range``
    for any other. Nor may it lie in ground a remembered hostile holds
    (``known_reach``), unless ``fight``: the planner chose to fight for it
    (a ``gather_gems`` op with ``fight``). Safe zones are not required.
    """
    if w.map_id is None or pos not in w.view.tiles:
        return False
    if w.view.tiles.get(pos) in policy.avoid_blocks:
        return False
    if any(is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= gather_bar(w, e, policy) for e in w.entities):
        return False
    return fight or not any(chebyshev(c, pos) <= r for c, r in known_reach(w, policy))


def known_reach(w: WorldModel, policy: Policy) -> list[tuple[Pos, int]]:
    """Ground known hostiles hold beyond where they stand in view: (centre, radius) each (free-play run 5).

    One that keeps ground (``Sighting.keeps_ground``: a post, or a place it
    hit us from) holds ``policy.hostile_range``, or the reach it hit us from
    when further, plus ``GATHER_SHADOW_MARGIN``, round its home, in view or
    not: it goes back there. One out of view also holds its ``gather_bar``
    round the cell it was last seen on.
    """
    if w.map_id is None:
        return []
    in_view = {(e.kind, e.id) for e in w.entities}
    out: list[tuple[Pos, int]] = []
    for key, s in w.sightings.items():
        if s.map_id != w.map_id or not is_hostile(w, policy, s.entity):
            continue
        if s.keeps_ground:
            out.append((s.home, max(policy.hostile_range, s.reach) + GATHER_SHADOW_MARGIN))
        if key not in in_view:
            out.append((s.entity.pos, gather_bar(w, s.entity, policy)))
    return out


def reach_cells(w: WorldModel, policy: Policy) -> set[Pos]:
    """Every cell in a known hostile's reach: ``hostile_reach`` of those in
    view and ``known_reach``. A walk prices them ``costly``."""
    cells = hostile_reach(w, policy)
    for (cx, cy), r in known_reach(w, policy):
        cells.update((cx + dx, cy + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1))
    return cells


def route_clear(w: WorldModel, policy: Policy, path: list[Pos]) -> bool:
    """No cell of ``path`` lies in a known hostile's reach (``reach_cells``).

    Reach that already holds where we stand is not counted: walking out of
    it is the way off. A target whose cheapest route still crosses reach is
    not free to take (free-play run 5: piles beside a post cost 12 hits).
    """
    here = w.pos
    skip = {
        (e.kind, e.id)
        for e in w.entities
        if here is not None and is_hostile(w, policy, e) and chebyshev(e.pos, here) <= policy.hostile_range
    }
    near = hostile_reach(w, policy, skip)
    zones = [(c, r) for c, r in known_reach(w, policy) if here is None or chebyshev(c, here) > r]
    return not any(p in near or any(chebyshev(c, p) <= r for c, r in zones) for p in path)


def near_respawn_anchor(w: WorldModel, pos: Pos) -> bool:
    if w.map_id is None:
        return False
    for map_id, anchor in w.respawn_anchors:
        if map_id == w.map_id and chebyshev(pos, anchor) <= RESPAWN_PROBE_RADIUS:
            return True
    return False


def touches_safe_zone(w: WorldModel, pos: Pos) -> bool:
    if w.map_id is None:
        return False
    safe = safe_tiles(w, w.map_id)
    if pos in safe:
        return True
    return any((pos[0] + dx, pos[1] + dy) in safe for dx, dy in NEIGHBOURS)


def is_safe_ish(w: WorldModel, pos: Pos, policy: Policy) -> bool:
    """Known safe-zone ground: what a hurt character walks first.

    The cell must be revealed, not on a hazard tile, and free of hostiles in
    range. It qualifies when it is a known safe zone, touches one, or lies
    within the respawn probe ring around town.
    """
    if w.map_id is None or pos not in w.view.tiles:
        return False
    if w.view.tiles.get(pos) in policy.avoid_blocks:
        return False
    if hostiles_near(w, pos, policy):
        return False
    return touches_safe_zone(w, pos) or near_respawn_anchor(w, pos)
