"""Which ground **Gather** may work, and which counts as safe-ish (A22). Not a ``STATES`` entry.

``GatherState`` targets a gem pile or grass only where ``gather_ground``
holds: known ground off hazards with no known hostile near, in view or
remembered (``hostile_ground.known_reach``), and walks there only by a route clear of every
known hostile's reach (``route_bar``, ``route_clear``). Detour (A71) uses the same two
tests for its finds. That includes the
open field, where the gems are, and safe zones, where breaking a block is
allowed; Gather learns which ground does not cut (``gem_yield``). Staying alive in the field is the reflexes' job (Retreat,
Flee, Fight, Heal), not Gather's. The safe-zone blocks a hurt character
walks first are ``hostile_ground.is_safe_ish`` (``safe_explore_path``).
"""

from __future__ import annotations

from agentrealm_adk.config import Policy
from agentrealm_adk.hostile_ground import Danger, danger, gather_bar, zone_cells
from agentrealm_adk.survival import hostile_reach, is_hostile
from agentrealm_adk.world import Entity, Pos, WorldModel, chebyshev


def gather_ground(w: WorldModel, pos: Pos, policy: Policy, d: Danger | None = None) -> bool:
    """Known ground where cutting grass, or taking a gem pile, is allowed.

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


def route_bar(w: WorldModel, policy: Policy, d: Danger | None = None) -> set[Pos]:
    """The cells a walk to a gem may not cross: every known hostile's reach,
    in view (``hostile_reach``) or remembered (``d.held``), none when
    ``d.fight``: the op chose the risk.

    Reach that already holds where we stand is not counted: walking out of
    it is the way off.
    """
    d = d or danger(w, policy)
    if d.fight:
        return set()
    here = w.pos
    skip = {
        (e.kind, e.id)
        for e in w.entities
        if here is not None and is_hostile(w, policy, e) and chebyshev(e.pos, here) <= policy.hostile_range
    }
    zones = [(c, r) for c, r in d.held if here is None or chebyshev(c, here) > r]
    return hostile_reach(w, policy, skip) | zone_cells(zones)


def route_clear(w: WorldModel, policy: Policy, path: list[Pos], d: Danger | None = None) -> bool:
    """No cell of ``path`` lies in ``route_bar``: a target whose cheapest
    route still crosses a known hostile's reach is not free to take."""
    bar = route_bar(w, policy, d)
    return not any(p in bar for p in path)
