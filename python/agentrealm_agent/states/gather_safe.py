"""Which ground **Gather** may work, and which counts as safe-ish (A22). Not a ``STATES`` entry.

``GatherState`` targets a gem pile, grass or bush only where ``gather_ground``
holds: known ground off hazards with no known hostile near, in view or
remembered (``hostile_ground.known_reach``), and walks there only by a route clear of every
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
from ..hostile_ground import Danger, danger, gather_bar
from ..survival import hostile_reach, is_hostile
from ..world import NEIGHBOURS, Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import RESPAWN_PROBE_RADIUS, safe_tiles


def hostiles_near(w: WorldModel, pos: Pos, policy: Policy, radius: int | None = None) -> bool:
    """A threat (``is_hostile``) in view within ``radius`` of ``pos``
    (``policy.hostile_range`` by default). Townsfolk are not one."""
    reach = policy.hostile_range if radius is None else radius
    return any(is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= reach for e in w.entities)


def gather_ground(w: WorldModel, pos: Pos, policy: Policy, d: Danger | None = None) -> bool:
    """Known ground where cutting grass or a bush, or taking a gem pile, is allowed.

    The cell must be revealed, not on a hazard tile, and have no known
    hostile in view within its ``gather_bar``: ``GATHER_HOSTILE_RADIUS`` for
    the one that hit us, a step past our weapon reach or ``policy.hostile_range``
    for any other. Nor may it lie in ground a remembered hostile holds
    (``d.held``, worked out here when ``d`` is None), unless the planner
    chose to fight for it (a ``gather_gems`` op with ``fight``). Safe zones
    are not required.
    """
    if w.map_id is None or pos not in w.view.tiles:
        return False
    if w.view.tiles.get(pos) in policy.avoid_blocks:
        return False
    if any(is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= gather_bar(w, e, policy) for e in w.entities):
        return False
    held = (d or danger(w, policy)).held
    return not any(chebyshev(c, pos) <= r for c, r in held)


def route_clear(w: WorldModel, policy: Policy, path: list[Pos], d: Danger | None = None) -> bool:
    """No cell of ``path`` lies in a known hostile's reach (``reach_cells``),
    or ``d.fight``: the op chose the risk.

    Reach that already holds where we stand is not counted: walking out of
    it is the way off. A target whose cheapest route still crosses reach is
    not free to take (free-play run 5: piles beside a post cost 12 hits).
    """
    d = d or danger(w, policy)
    if d.fight:
        return True
    here = w.pos
    skip = {
        (e.kind, e.id)
        for e in w.entities
        if here is not None and is_hostile(w, policy, e) and chebyshev(e.pos, here) <= policy.hostile_range
    }
    near = hostile_reach(w, policy, skip)
    zones = [(c, r) for c, r in d.held if here is None or chebyshev(c, here) > r]
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
