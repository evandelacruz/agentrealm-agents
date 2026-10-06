"""Which ground **Gather** may work, and which counts as safe-ish (A22). Not a ``STATES`` entry.

``GatherState`` takes a gem pile only on ``open_ground``: known ground off
hazards with no known hostile near. It cuts grass or a bush only on
``gather_ground``: open ground outside town (``town_cells``). Breaking a
block is allowed in a safe zone, but a cut on town grass was observed to
have no effect (GAME_NOTES.md Movement and blocks, A63 run 2), and gems drop
from cuts outside town, so the open field is where Gather works; other safe
zones stay cuttable, with the no-effect hold (``gem_yield``) as backstop.
Staying alive in the field is the reflexes' job (Retreat, Flee, Fight,
Heal), not Gather's. ``is_safe_ish``
is the stricter safe-zone ground a hurt character walks first
(``safe_explore_path``).
"""

from __future__ import annotations

from typing import Collection

from ..config import Policy
from ..navigation.planner import HOSTILE_DANGER_RADIUS
from ..world import NEIGHBOURS, Pos, WorldModel, chebyshev
from ..zone_discovery import RESPAWN_PROBE_RADIUS, safe_tiles


# Gather keeps this far from any known hostile: the radius inside which the
# path planner already prices a hostile's cells as dangerous.
GATHER_HOSTILE_RADIUS = HOSTILE_DANGER_RADIUS


def hostiles_near(w: WorldModel, pos: Pos, policy: Policy, radius: int | None = None) -> bool:
    """A hostile in view within ``radius`` of ``pos`` (``policy.hostile_range`` by default)."""
    reach = policy.hostile_range if radius is None else radius
    return any(e.kind in policy.hostile and chebyshev(e.pos, pos) <= reach for e in w.entities)


def open_ground(w: WorldModel, pos: Pos, policy: Policy) -> bool:
    """Known ground where taking a gem pile is allowed.

    The cell must be revealed, not on a hazard tile, and have no known
    hostile within ``GATHER_HOSTILE_RADIUS``.
    """
    if w.map_id is None or pos not in w.view.tiles:
        return False
    if w.view.tiles.get(pos) in policy.avoid_blocks:
        return False
    return not hostiles_near(w, pos, policy, GATHER_HOSTILE_RADIUS)


def gather_ground(w: WorldModel, pos: Pos, policy: Policy, town: Collection[Pos]) -> bool:
    """Known ground where cutting grass or a bush is worth a ``Use``: open
    ground outside ``town`` (``town_cells``, read once per decision)."""
    return pos not in town and open_ground(w, pos, policy)


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
