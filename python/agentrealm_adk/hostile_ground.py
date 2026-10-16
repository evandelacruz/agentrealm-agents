"""The ground known hostiles hold: one threat picture for every walk that keeps clear of them (A22, A9, A162).

A hostile keeps no post (Manual §11 Hostiles): it goes for the nearest
living character on its map, with no sight or distance limit. A chaser steps
to the neighbouring block that brings it closest and stands still where none
does, so a wall or water stops it; a patroller walks its loop; a zone spawn
never leaves its zone; none heals or walks back to its spawn. The agent sees
neither a hostile's kind nor its zone, so it prices every hostile as a
chaser free to follow (``chase``): the one that costs most.

A hostile in view reaches ``policy.hostile_range`` round where it stands
(``survival.hostile_reach``). One remembered out of view
(``WorldModel.sightings``) holds its ``gather_bar`` round the cell it was
last seen on (``known_reach``, free-play run 5): for ``SIGHTING_TICKS`` when
it was moving, and until that cell is seen empty when it stood still there
(blocked, ``Sighting``). ``ground_by_hostile`` puts both together per hostile.

Safety is judged on the exact block (Manual §11 Combat): no hit, damaging
block or trap hurts on a safe-zone block, so a known safe block is no
hostile's ground (``off_safe``), and the only refuge a hurt character walks
to first (``is_safe_ish``).

Gather and Detour keep their targets and routes off this ground
(``states/gather_safe.py``). Ground near what a hostile holds, or near one
in view, is priced, never barred (``near_reach``): a target there may draw
it in, so Gather and Detour count its steps. Retreat, Park and Heal pick a
safe cell, and Flee runs toward the cell Retreat picked, so the two never
pull opposite ways (``pathing.retreat_safe_goal``, free-play run 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Callable

from .navigation.planner import HOSTILE_DANGER_RADIUS
from .survival import hostile_reach, is_attacker, is_hostile, recently_attacked
from .world import NEIGHBOURS, SIGHTING_TICKS, Entity, Pos, Sighting, WorldModel, chebyshev
from .zone_discovery import known_safe, safe_tiles

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
# Ground within this many steps of what a known hostile holds, or of one in
# view, is priced at ``NEAR_GROUND_STEPS`` a cell, never barred: a target or
# a walk there may draw it in. The radius inside which the path planner
# prices a hostile's cells as dangerous.
NEAR_GROUND_RADIUS = GATHER_HOSTILE_RADIUS
NEAR_GROUND_STEPS = 6
# A walk on an op's behalf keeps this many steps clear of a seen hostile's
# ``policy.hostile_range`` (``walk_reach``, A128).
WALK_REACH_MARGIN = GATHER_SHADOW_MARGIN

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


def off_safe(w: WorldModel, cells: set[Pos]) -> set[Pos]:
    """``cells`` less the known safe blocks on this map: nothing hurts on
    one (Manual §11 Combat), so it is no hostile's ground."""
    if w.map_id is None or not cells:
        return cells
    return cells - safe_tiles(w, w.map_id)


@dataclass(frozen=True)
class Danger:
    """What one decision knows of hostile ground, worked out once (``danger``):
    ``held``, the ground remembered hostiles hold (``known_reach``),
    ``priced``, ground with the steps it adds to a cell in it (``near_reach``:
    near what a hostile holds or where one in view stands), and ``fight``,
    the op chose to fight for its ground, so remembered ground and routes
    bar nothing."""

    held: tuple[tuple[Pos, int], ...] = ()
    priced: tuple[tuple[Pos, int, int], ...] = ()
    fight: bool = False

    def price(self, pos: Pos) -> int:
        """Steps the priced ground covering ``pos`` adds to it: the dearest
        zone's, so it stays at most ``NEAR_GROUND_STEPS``."""
        return max((steps for c, r, steps in self.priced if chebyshev(c, pos) <= r), default=0)

    def priced_cells(self) -> dict[Pos, int]:
        """Every cell priced ground covers, with its ``price``: a walk through
        it costs that much more, as a target in it does."""
        out: dict[Pos, int] = {}
        for c, r, steps in self.priced:
            for p in zone_cells([(c, r)]):
                if out.get(p, 0) < steps:
                    out[p] = steps
        return out


def danger(w: WorldModel, policy: Policy, fight: bool = False) -> Danger:
    """This decision's ``Danger``: nothing held or priced when ``fight``, else
    ``known_reach`` and ``near_reach``."""
    if fight:
        return Danger(fight=True)
    held = known_reach(w, policy)
    return Danger(tuple(held), tuple(near_reach(w, policy, held)))


def near_reach(w: WorldModel, policy: Policy, held: list[tuple[Pos, int]]) -> list[tuple[Pos, int, int]]:
    """The ground within ``NEAR_GROUND_RADIUS`` of ``held`` zones and of every
    hostile in view's ``hostile_range``, each cell at ``NEAR_GROUND_STEPS``."""
    zones = held + [(e.pos, policy.hostile_range) for e in w.entities if is_hostile(w, policy, e)]
    return [(c, r + NEAR_GROUND_RADIUS, NEAR_GROUND_STEPS) for c, r in zones]


def held_by_hostile(w: WorldModel, policy: Policy) -> dict[HostileKey, list[tuple[Pos, int]]]:
    """Ground each remembered hostile out of view holds: its ``gather_bar``
    round the cell it was last seen on, while that cell still holds
    (``_last_seen_holds``), as (centre, radius) zones per (kind, id)
    (free-play run 5). A hostile in view holds where it stands, not here."""
    if w.map_id is None:
        return {}
    in_view = {(e.kind, e.id) for e in w.entities}
    out: dict[HostileKey, list[tuple[Pos, int]]] = {}
    for key, s in w.sightings.items():
        if key in in_view or s.map_id != w.map_id or not is_hostile(w, policy, s.entity):
            continue
        if _last_seen_holds(w, s):
            out[key] = [(s.entity.pos, gather_bar(w, s.entity, policy))]
    return out


def _last_seen_holds(w: WorldModel, s: Sighting) -> bool:
    """The cell ``s`` was last seen on still holds ground: that cell is not
    in sight with it gone, and it stood still there (blocked: time alone
    does not move it) or was seen there within ``SIGHTING_TICKS``."""
    in_sight = w.pos is not None and chebyshev(s.entity.pos, w.pos) < w.perception
    return not in_sight and (s.still or w.tick - s.tick <= SIGHTING_TICKS)


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
    view and ``d.held``, known safe blocks left out (``off_safe``). A walk
    prices them ``costly``."""
    return off_safe(w, hostile_reach(w, policy) | zone_cells((d or danger(w, policy)).held))


def chaser_step(at: Pos, target: Pos, ground: Callable[[Pos], bool]) -> Pos:
    """Where a chaser on ``at`` stands after one step toward ``target``
    (Manual §11 Hostiles): the neighbouring block that brings it closest,
    nearest by Chebyshev distance then straight-line, never onto ``target``
    itself, and ``at`` when no step on ``ground`` brings it closer. It finds
    no path, so a wall or water between stops it."""
    def gap(p: Pos) -> tuple[int, int]:
        return chebyshev(p, target), (p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2

    best, best_gap = at, gap(at)
    for dx, dy in NEIGHBOURS:
        n = (at[0] + dx, at[1] + dy)
        if n != target and ground(n) and gap(n) < best_gap:
            best, best_gap = n, gap(n)
    return best


def chase(w: WorldModel, start: Pos, path: Sequence[Pos]) -> list[Pos]:
    """Where a chaser that stands on ``start`` is as we stand on each cell of
    ``path`` in turn: one ``chaser_step`` toward each, on the ground a
    hostile walks (``WorldModel.chaser_ground``). It goes for the nearest
    character, which is priced as us, at our pace: the Manual publishes no
    hostile's speed. So a chaser in the open keeps up with a walk past it,
    and only one the ground stops (blocked) can be passed."""
    ground = w.chaser_ground()
    at, out = start, []
    for p in path:
        at = chaser_step(at, p, ground)
        out.append(at)
    return out


@dataclass(frozen=True)
class WalkGround:
    """One known hostile's ground a walk on an op's behalf keeps out of
    (``walk_ground``): ``entity`` as in view, or as last seen when
    remembered (where a chase of it starts, ``chase``), ``cells``, and
    ``radius``, how far round where it stands its ground reaches."""

    entity: Entity
    in_view: bool
    cells: frozenset[Pos]
    radius: int


def walk_ground(w: WorldModel, policy: Policy, skip: Collection[HostileKey] = ()) -> dict[HostileKey, WalkGround]:
    """Each known hostile's ground a walk on an op's behalf keeps out of
    (A128), by (kind, id): within ``policy.hostile_range`` plus
    ``WALK_REACH_MARGIN`` of every hostile in view but those in ``skip``,
    and of each remembered one out of view, what it holds round the cell it
    was last seen on (``held_by_hostile``), known safe blocks left out
    (``off_safe``). ``skip`` is the hostiles the engagement would fight
    (``pathing.op_walk_sets``): those are left to it."""
    out: dict[HostileKey, WalkGround] = {}
    for e in w.entities:
        if is_hostile(w, policy, e) and (e.kind, e.id) not in skip:
            radius = policy.hostile_range + WALK_REACH_MARGIN
            out[(e.kind, e.id)] = WalkGround(e, True, frozenset(off_safe(w, zone_cells([(e.pos, radius)]))), radius)
    for key, zones in held_by_hostile(w, policy).items():
        cells = frozenset(off_safe(w, zone_cells(zones)))
        out[key] = WalkGround(w.sightings[key].entity, False, cells, max(r for _, r in zones))
    return out


def walk_reach(w: WorldModel, policy: Policy, skip: Collection[HostileKey] = ()) -> set[Pos]:
    """The ground a walk on an op's behalf keeps out of (A128): every
    ``walk_ground`` together. The walk prices it ``costly`` so it routes
    round a pack it has seen, and one whose only way crosses it passes it
    or asks the planner (``pathing.guided_step``, A146, A151)."""
    return {p for g in walk_ground(w, policy, skip).values() for p in g.cells}


def ground_by_hostile(w: WorldModel, policy: Policy) -> dict[HostileKey, set[Pos]]:
    """Each known hostile, (kind, id), with the ground it holds: its
    ``hostile_reach`` when in view, and its ``held_by_hostile`` zones out of
    view. A refuge is judged by its approach (``pathing.retreat_safe_goal``):
    a safe block hurts nobody standing on it (Manual §11 Combat), but one a
    hostile gets to first is walked into its hits (free-play run 7)."""
    out: dict[HostileKey, set[Pos]] = {}
    for e in w.entities:
        if is_hostile(w, policy, e):
            out[(e.kind, e.id)] = hostile_reach(w, policy, only=e)
    for key, zones in held_by_hostile(w, policy).items():
        out.setdefault(key, set()).update(zone_cells(zones))
    return out


def hostiles_within(w: WorldModel, policy: Policy, pos: Pos, radius: int) -> list[Entity]:
    """The known hostiles within ``radius`` of ``pos``: one in view where it
    stands, one remembered out of view where it was last seen while that
    cell holds (``held_by_hostile``)."""
    out: dict[HostileKey, Entity] = {
        (e.kind, e.id): e for e in w.entities if is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= radius
    }
    for key in held_by_hostile(w, policy):
        e = w.sightings[key].entity
        if key not in out and chebyshev(e.pos, pos) <= radius:
            out[key] = e
    return list(out.values())


def hostiles_near(w: WorldModel, pos: Pos, policy: Policy, radius: int | None = None) -> bool:
    """A threat (``is_hostile``) in view within ``radius`` of ``pos``
    (``policy.hostile_range`` by default). Townsfolk are not one."""
    reach = policy.hostile_range if radius is None else radius
    return any(is_hostile(w, policy, e) and chebyshev(e.pos, pos) <= reach for e in w.entities)


def is_safe_ish(w: WorldModel, pos: Pos) -> bool:
    """A known safe-zone block: what a hurt character walks first. Safety is
    judged on the exact block (Manual §11 Combat): a hostile may walk in but
    deals nothing there, and damaging blocks and traps do not hurt, so
    neither a hostile near nor a hazard tile makes it less safe, and a block
    beside a safe zone is not safe at all."""
    return w.map_id is not None and known_safe(w, w.map_id, pos)
