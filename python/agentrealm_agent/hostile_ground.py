"""The ground known hostiles hold: one threat picture for every walk that keeps clear of them (A22, A9).

A hostile in view reaches ``policy.hostile_range`` round where it stands
(``survival.hostile_reach``). One remembered out of view (``WorldModel.sightings``)
still holds ground: round its post, and round the cell it was last seen on
(``known_reach``, free-play run 5). ``ground_by_hostile`` puts both together
per hostile.

Gather and Detour keep their targets and routes off this ground
(``states/gather_safe.py``). A remembered post fades (``Sighting.strength``,
free-play run 9): once faded it holds no ground, only prices it, so Gather
cuts its grass when no free grass is near (``Danger.price``). Retreat, Park
and Heal pick a safe cell only outside it, and Flee runs toward the cell
Retreat picked, so the two never pull opposite ways
(``pathing.retreat_safe_goal``, free-play run 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .navigation.planner import HOSTILE_DANGER_RADIUS
from .survival import hostile_reach, is_attacker, is_hostile, recently_attacked
from .world import POST_HOLD_STRENGTH, SIGHTING_TICKS, Entity, Pos, Sighting, WorldModel, chebyshev

if TYPE_CHECKING:
    from .config import Policy

# Gather keeps this far from the hostile that hit us: the radius inside which
# the path planner already prices a hostile's cells as dangerous.
GATHER_HOSTILE_RADIUS = HOSTILE_DANGER_RADIUS
# A hostile that has not hit us bars only cells within our weapon reach of it,
# or ``policy.hostile_range`` when that is further, plus this many steps: one
# that followed at 4–6 blocks without attacking stopped all cutting for 45 s
# under the full radius (A63 run 3).
GATHER_SHADOW_MARGIN = 1
# A post's reach counts at most this far from it, however far its guard
# chased us before a hit: a reach learned from a long chase walled off the
# grass round town (free-play run 9, A85).
POST_REACH_CAP = GATHER_HOSTILE_RADIUS
# The most a faded post (below ``POST_HOLD_STRENGTH``) adds to a cell Gather
# would work inside its ground, in steps of walk: it prices that grass, so
# free grass this much further off is picked first, and never bars it.
FADED_POST_STEPS = 6

HostileKey = tuple[str, int]


def gather_bar(w: WorldModel, e: Entity, policy: Policy) -> int:
    """How far from threat ``e`` (``is_hostile``) Gather keeps the cells it works.

    Never inside ``policy.hostile_range``, where Retreat, Flee and Fight
    start, and a step clear of it: a bar shorter than that range sent Gather
    to a cell Retreat walked straight back from, 4 times in 34 s (A63 run 4).
    """
    if recently_attacked(w) and is_attacker(w, e):
        return max(GATHER_HOSTILE_RADIUS, policy.hostile_range + GATHER_SHADOW_MARGIN)
    return max(w.attack_range or 1, policy.hostile_range) + GATHER_SHADOW_MARGIN


@dataclass(frozen=True)
class Danger:
    """What one decision knows of hostile ground, worked out once (``danger``):
    ``held``, the ground remembered hostiles hold (``known_reach``),
    ``priced``, the ground of faded posts with the steps each adds to a cell
    in it (``faded_reach``), and ``fight``, the op chose to fight for its
    ground, so remembered ground and routes bar nothing."""

    held: tuple[tuple[Pos, int], ...] = ()
    priced: tuple[tuple[Pos, int, int], ...] = ()
    fight: bool = False

    def price(self, pos: Pos) -> int:
        """Steps the faded posts whose ground covers ``pos`` add to it: the
        dearest one's, so it stays at most ``FADED_POST_STEPS``."""
        return max((steps for c, r, steps in self.priced if chebyshev(c, pos) <= r), default=0)


def danger(w: WorldModel, policy: Policy, fight: bool = False) -> Danger:
    """This decision's ``Danger``: nothing held or priced when ``fight``, else
    ``known_reach`` and ``faded_reach``."""
    if fight:
        return Danger(fight=True)
    return Danger(tuple(known_reach(w, policy)), tuple(faded_reach(w, policy)))


def held_by_hostile(
    w: WorldModel, policy: Policy, faded: bool = False
) -> dict[HostileKey, list[tuple[Pos, int]]]:
    """Ground each remembered hostile holds beyond where it stands in view:
    (centre, radius) zones per (kind, id) (free-play run 5).

    One that keeps a post (``Sighting.post``) holds ``policy.hostile_range``,
    or the reach it hit us from when further (up to ``POST_REACH_CAP``), plus
    ``GATHER_SHADOW_MARGIN``, round that post, in view or not: it goes back
    there. One out of view also holds its ``gather_bar`` round the cell it
    was last seen on. A post that has faded below ``POST_HOLD_STRENGTH``
    (``Sighting.strength``, free-play run 9) holds neither, unless its guard
    was seen within ``SIGHTING_TICKS`` somewhere not in sight now: then its
    last-seen cell holds, as a passer-by's does. ``faded`` lists the faded zones instead, which only
    price ground (``faded_reach``).
    """
    if w.map_id is None:
        return {}
    in_view = {(e.kind, e.id) for e in w.entities}
    out: dict[HostileKey, list[tuple[Pos, int]]] = {}
    for key, s in w.sightings.items():
        if s.map_id != w.map_id or not is_hostile(w, policy, s.entity):
            continue
        zones = []
        if s.post and _post_faded(s) == faded:
            reach = max(policy.hostile_range, min(s.reach, POST_REACH_CAP))
            zones.append((s.home, reach + GATHER_SHADOW_MARGIN))
        if key not in in_view and _last_seen_faded(w, s) == faded:
            zones.append((s.entity.pos, gather_bar(w, s.entity, policy)))
        if zones:
            out[key] = zones
    return out


def _post_faded(s: Sighting) -> bool:
    """``s`` keeps a post that has faded below ``POST_HOLD_STRENGTH``."""
    return s.post and s.strength < POST_HOLD_STRENGTH


def _last_seen_faded(w: WorldModel, s: Sighting) -> bool:
    """The cell ``s`` was last seen on holds no ground: its post has faded,
    and its guard has not been seen for ``SIGHTING_TICKS`` or that cell is
    in sight with it gone, as a passer-by is forgotten."""
    if not _post_faded(s):
        return False
    in_sight = w.pos is not None and chebyshev(s.entity.pos, w.pos) < w.perception
    return in_sight or w.tick - s.tick > SIGHTING_TICKS


def faded_reach(w: WorldModel, policy: Policy) -> list[tuple[Pos, int, int]]:
    """The zones of faded posts (``held_by_hostile`` with ``faded``), each
    with the steps it adds to a cell in it: ``FADED_POST_STEPS`` scaled by
    how strong the post still is against ``POST_HOLD_STRENGTH``, at least 1."""
    out = []
    for key, zones in held_by_hostile(w, policy, faded=True).items():
        s = w.sightings[key]
        steps = max(1, round(FADED_POST_STEPS * s.strength / POST_HOLD_STRENGTH))
        out.extend((c, r, steps) for c, r in zones)
    return out


def known_reach(w: WorldModel, policy: Policy) -> list[tuple[Pos, int]]:
    """Every zone of ``held_by_hostile``, as one list of (centre, radius)."""
    return [zone for zones in held_by_hostile(w, policy).values() for zone in zones]


def zone_cells(zones: list[tuple[Pos, int]] | tuple[tuple[Pos, int], ...]) -> set[Pos]:
    """The cells inside (centre, radius) ``zones``."""
    cells: set[Pos] = set()
    for (cx, cy), r in zones:
        cells.update((cx + dx, cy + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1))
    return cells


def reach_cells(w: WorldModel, policy: Policy, d: Danger | None = None) -> set[Pos]:
    """Every cell in a known hostile's reach: ``hostile_reach`` of those in
    view and ``d.held``. A walk prices them ``costly``."""
    return hostile_reach(w, policy) | zone_cells((d or danger(w, policy)).held)


def ground_by_hostile(w: WorldModel, policy: Policy) -> dict[HostileKey, set[Pos]]:
    """Each known hostile, (kind, id), with the ground it holds: its
    ``hostile_reach`` when in view, and its ``held_by_hostile`` zones.

    A safe cell inside any of it is no refuge (free-play run 7: Retreat
    walked to a safe cell 2 from the hostile it ran from and took 6 hits).
    """
    out: dict[HostileKey, set[Pos]] = {}
    for e in w.entities:
        if is_hostile(w, policy, e):
            out[(e.kind, e.id)] = hostile_reach(w, policy, only=e)
    for key, zones in held_by_hostile(w, policy).items():
        out.setdefault(key, set()).update(zone_cells(zones))
    return out


def hostiles_within(w: WorldModel, policy: Policy, pos: Pos, radius: int) -> list[Entity]:
    """The known hostiles within ``radius`` of ``pos``: one in view where it
    stands; one remembered (``WorldModel.sightings``) at its post, and out of
    view also where it was last seen. Ground a faded post no longer holds is
    not counted (``held_by_hostile``, A85)."""
    out: dict[HostileKey, Entity] = {
        (e.kind, e.id): e for e in w.entities if is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= radius
    }
    in_view = {(e.kind, e.id) for e in w.entities}
    for key, s in w.sightings.items():
        if key in out or s.map_id != w.map_id or not is_hostile(w, policy, s.entity):
            continue
        cells = ([s.home] if s.post and not _post_faded(s) else []) + (
            [s.entity.pos] if key not in in_view and not _last_seen_faded(w, s) else []
        )
        if any(chebyshev(c, pos) <= radius for c in cells):
            out[key] = s.entity
    return list(out.values())
