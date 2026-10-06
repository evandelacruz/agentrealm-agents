"""Gem yield per region, learned from the agent's own cuts (A63).

Gem drops from cutting grass and bushes vary by area, and some areas drop
none. Nothing here knows where: every number comes from cuts this agent made.

1. **Measure.** Each block our ``Use`` cut is one record: the cell, its block
   type, the tick, and whether a gem came of it. The drop is tied to the cut
   by a free ``gem`` supply that appears on or next to the cell within
   ``GEM_WINDOW_TICKS`` (one that was not in view at the cut and no earlier
   cut claimed), or by the gem counter rising in that window with no gem
   ``Take`` applied since the cut (:func:`take_raises_gems`). Cuts still
   waiting at a death or a map change are dropped, not filed.
2. **Summarise.** Cells fall in ``REGION_SIZE`` square regions per map. Each
   region keeps its cuts, gems and last tick of grass and bush cuts. A region
   with ``BARREN_MIN_CUTS`` or more such cuts and no gem is barren.

Stored in the world knowledge base under ``kb.extra["gem_yield"]``::

    {"<map_id>": {"cuts": [{"x", "y", "block", "tick", "gem"}, ...],
                  "regions": {"<rx>,<ry>": {"cuts", "gems", "last_tick"}}}}

``cuts`` keeps the latest ``MAX_RECORDS`` per map; ``regions`` keeps every
total. The planner's State shows :func:`summary`; Gather skips barren
regions (:func:`barren_regions`) and cells it cut within ``REGROW_TICKS``,
pending cuts included (:func:`exhausted_cells`), both read once per decision.

A ``Use`` that comes back ``applied_no_effect`` cut nothing: there was no
roll, so it is never filed (a false miss would push its region toward
barren). The tracker holds that cell out for ``REGROW_TICKS`` so Gather
moves on, and counts it for the planner (:meth:`GemYieldTracker.run_counts`).
After ``NO_EFFECT_ZONE_CUTS`` such cuts in one region, or in one known safe
zone (repeats on one cell count), the whole region or zone is uncuttable
until those cuts are ``NO_EFFECT_TTL`` old (:meth:`GemYieldTracker.uncuttable`). That is not barren: barren means cuts
work and drop no gem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .break_memory import BREAKABLE
from .knowledge_base import KnowledgeBase
from .world import Pos, WorldModel, chebyshev
from .zone_discovery import safe_tiles, safe_zone_of

KEY = "gem_yield"
REGION_SIZE = 16  # blocks per region side
# A fair sample: this many cuts with no gem marks a region barren. At the
# manual's lowest rate (10% a cut, GAME_NOTES.md Gems) a normal region shows
# no gem in 30 cuts about 4% of the time (0.9**30); at 15 it would be 21%.
BARREN_MIN_CUTS = 30
GEM_WINDOW_TICKS = 5  # a gem that shows up later than this is not the cut's
MAX_RECORDS = 500  # cut records kept per map; region totals are kept in full
# A cut block shows its destroyed type until it grows back; a cut bush took
# 600 ticks (GAME_NOTES.md Movement and blocks). A cell cut more recently than this
# is exhausted, whatever a stale terrain read still says.
REGROW_TICKS = 600
CUT_BLOCKS = BREAKABLE | {"grass"}  # blocks a cut or break is recorded on
GATHER_BLOCKS = frozenset({"grass", "bush"})  # the cuts the region yield counts
SUMMARY_RADIUS = 3  # barren regions this far (Chebyshev, in regions) from ours are shown
# Best regions shown to the planner, at any distance: walking out of range of
# the one productive region must not hide it (A63 run 3).
SUMMARY_BEST = 3
# This many no-effect cuts in one region, or one safe zone, mark it uncuttable.
NO_EFFECT_ZONE_CUTS = 3
MAX_NO_EFFECT = 500  # no-effect cuts remembered, oldest dropped first
# A no-effect cut is forgotten this long after it, so an uncuttable mark lapses
# and the ground is tried again (it holds the cell out only REGROW_TICKS).
NO_EFFECT_TTL = 10 * REGROW_TICKS
# Gather leaves poor ground for a better known region (free-play run 2: 54
# cuts for 1 gem in one region while the next one over gave 1 gem in 4).
# A region with at least FAIR_SAMPLE_CUTS cuts and fewer than POOR_YIELD gems
# a cut is poor: half the manual's lowest drop rate (10% a cut, GAME_NOTES.md Gems).
FAIR_SAMPLE_CUTS = 20
POOR_YIELD = 0.05
# A region is better when it gave at least GOOD_YIELD gems a cut over at least
# GOOD_MIN_CUTS cuts, and lies within BETTER_REGION_BLOCKS of us; the planner
# can name a region at any distance with a ``gather_gems`` x, y.
GOOD_YIELD = 0.10
GOOD_MIN_CUTS = 4
BETTER_REGION_BLOCKS = 64
GEM_CODE = "gem"  # a ground gem (GAME_NOTES.md Gems)
GEM_CACHE_PREFIX = "gem_cache"  # gem_cache_5/7/10 (GAME_NOTES.md Gems)


def take_raises_gems(code: str | None) -> bool:
    """A ``Take`` that can raise the gem counter: a gem, a gem cache, or a
    supply whose code we could not read. Food or gear does not."""
    return code is None or code == GEM_CODE or code.startswith(GEM_CACHE_PREFIX)


def region_of(pos: Pos) -> tuple[int, int]:
    return pos[0] // REGION_SIZE, pos[1] // REGION_SIZE


def region_key(pos: Pos) -> str:
    rx, ry = region_of(pos)
    return f"{rx},{ry}"


def _ground_gems(w: WorldModel) -> list[tuple[int, Pos]]:
    """Free ground gems in view: (id, cell). A priced ``gem`` is shop stock."""
    return [(e.id, e.pos) for e in w.entities if e.kind == "supply" and e.code == GEM_CODE and e.gem_price is None]


@dataclass
class PendingCut:
    """A cut waiting out its window for a gem."""

    map_id: int
    pos: Pos
    block: str
    tick: int
    gems_before: int | None
    gems_in_view: set[int]
    took: bool = False  # a Take applied since: a counter rise is not ours
    gem: bool = False


@dataclass
class GemYieldTracker:
    """Ties gem drops to the cuts that made them, then files each cut (one per runner)."""

    pending: list[PendingCut] = field(default_factory=list)
    claimed: set[int] = field(default_factory=set)  # ground gems already credited to a cut
    on_ground: set[int] = field(default_factory=set)  # claimed gems not yet gone from view
    last_gems: int | None = None  # the gem counter at the last update
    # Uses that left grass or a bush unchanged (``applied_no_effect``), one
    # (map_id, cell, tick) per cut, oldest first, at most MAX_NO_EFFECT and
    # forgotten after NO_EFFECT_TTL. Each holds its cell out of Gather for
    # REGROW_TICKS; NO_EFFECT_ZONE_CUTS of them mark ground uncuttable.
    no_effect: list[tuple[int, Pos, int]] = field(default_factory=list)
    cuts: int = 0  # Uses on grass or a bush that took effect, this run
    no_effect_cuts: int = 0  # Uses on grass or a bush that did nothing, this run
    # The latest no-effect cut (map_id, cell, tick), cleared by a cut that takes effect.
    last_no_effect: tuple[int, Pos, int] | None = None
    gems_gained: int = 0  # rises of the gem counter this run, spending not taken off
    last_cut_tick: int | None = None  # tick of this run's latest cut that took effect: Gather's stall clock

    def note_cut(self, w: WorldModel, pos: Pos, block: str, tick: int, *, took: bool = False) -> None:
        """Our ``Use`` on ``pos`` applied while it showed ``block``. ``took``: a
        ``Take`` applied earlier in the same response, whose gem the counter
        does not show yet."""
        if w.map_id is None or not block:
            return
        if block in GATHER_BLOCKS:
            self.cuts += 1
            self.last_no_effect = None
            self.last_cut_tick = tick
        self.pending.append(
            PendingCut(w.map_id, pos, block, tick, w.gems, {gid for gid, _ in _ground_gems(w)}, took=took)
        )

    def note_no_effect(self, w: WorldModel, pos: Pos, block: str, tick: int) -> None:
        """Our ``Use`` on ``pos`` came back ``applied_no_effect`` while it showed
        ``block``. Grass or a bush: held out of Gather, never filed as a cut."""
        if w.map_id is None or block not in GATHER_BLOCKS:
            return
        self.no_effect.append((w.map_id, pos, tick))
        self._prune(tick)
        del self.no_effect[:-MAX_NO_EFFECT]
        self.no_effect_cuts += 1
        self.last_no_effect = (w.map_id, pos, tick)

    def pending_cells(self, map_id: int | None, tick: int) -> set[Pos]:
        """Cells of ``map_id`` Gather should not cut now, though not filed:
        cuts still waiting out their gem window, and cells a ``Use`` left
        unchanged within ``REGROW_TICKS`` of ``tick`` (:func:`exhausted_cells`)."""
        out = {c.pos for c in self.pending if c.map_id == map_id}
        return out | {p for mid, p, t in self.no_effect if mid == map_id and tick - t < REGROW_TICKS}

    def uncuttable(self, w: WorldModel, safe: set[Pos] | None = None) -> tuple[set[tuple[int, int]], set[Pos]]:
        """Regions, and known safe-zone cells, of ``w``'s map where
        ``NO_EFFECT_ZONE_CUTS`` or more cuts had no effect: (regions, cells).
        A safe zone counts as one, however many regions it spans. ``safe`` is
        ``safe_tiles`` when the caller already has it."""
        if w.map_id is None:
            return set(), set()
        self._prune(w.tick)
        here = [p for mid, p, _ in self.no_effect if mid == w.map_id]  # one per cut
        if not here:
            return set(), set()
        safe = safe_tiles(w, w.map_id) if safe is None else safe
        per_region: dict[tuple[int, int], int] = {}
        for p in here:
            if p not in safe:
                per_region[region_of(p)] = per_region.get(region_of(p), 0) + 1
        regions = {r for r, n in per_region.items() if n >= NO_EFFECT_ZONE_CUTS}
        cells: set[Pos] = set()
        seen: set[Pos] = set()
        for p in here:
            if p not in safe or p in seen:
                continue
            zone = safe_zone_of(w, w.map_id, p, safe)
            seen |= zone
            if sum(1 for q in here if q in zone) >= NO_EFFECT_ZONE_CUTS:
                cells |= zone
        return regions, cells

    def _prune(self, tick: int) -> None:
        """Forget no-effect cuts older than ``NO_EFFECT_TTL`` (oldest first)."""
        while self.no_effect and tick - self.no_effect[0][2] >= NO_EFFECT_TTL:
            del self.no_effect[0]

    def no_effect_near(self, map_id: int | None, pos: Pos, tick: int) -> bool:
        """The latest cut had no effect, in ``pos``'s region, and no cut took
        effect since, within ``REGROW_TICKS``: part of Gather's ``cuts have no
        effect here`` status."""
        last = self.last_no_effect
        if last is None or tick - last[2] >= REGROW_TICKS:
            return False
        return last[0] == map_id and region_of(last[1]) == region_of(pos)

    def run_counts(self) -> dict[str, int]:
        """This run's cuts, no-effect cuts and gems gained, for the planner's State."""
        return {"cuts": self.cuts, "no_effect_cuts": self.no_effect_cuts, "gems_gained": self.gems_gained}

    def note_take(self) -> None:
        for cut in self.pending:
            cut.took = True

    def update(self, w: WorldModel, kb: KnowledgeBase | None) -> None:
        """After a round trip is applied: credit gems, file cuts whose window closed."""
        rise = 0 if self.last_gems is None or w.gems is None else w.gems - self.last_gems
        self.last_gems = w.gems
        self.gems_gained += max(rise, 0)
        if not self.pending:
            return
        # Died, off the map, or on another map before the window ran: not a
        # fair sample, so not filed (a false miss would push toward barren).
        self.pending = [c for c in self.pending if w.alive and c.map_id == w.map_id]
        if not self.pending:
            self.claimed.clear()
            self.on_ground.clear()
            return
        gems = _ground_gems(w)
        for gone in sorted(self.on_ground - {gid for gid, _ in gems}):
            # A credited ground gem left view. Only when the counter rose with it
            # did it go into our counter: that rise is not another cut's.
            self.on_ground.discard(gone)
            if rise > 0:
                rise -= 1
                self._raise_baselines(None)
        for cut in self.pending:
            if cut.gem:
                continue
            fresh = [
                gid for gid, p in gems if gid not in cut.gems_in_view and gid not in self.claimed and chebyshev(p, cut.pos) <= 1
            ]
            if fresh:
                # A ground gem is not in the counter yet: baselines move when it leaves view.
                self.claimed.add(min(fresh))
                self.on_ground.add(min(fresh))
                cut.gem = True
            elif not cut.took and cut.gems_before is not None and w.gems is not None and w.gems > cut.gems_before:
                cut.gem = True
                self._raise_baselines(cut)  # one counter rise credits one cut
        keep: list[PendingCut] = []
        for cut in self.pending:
            if cut.gem or w.tick > cut.tick + GEM_WINDOW_TICKS:
                record_cut(kb, cut.map_id, cut.pos, cut.block, cut.tick, cut.gem)
            else:
                keep.append(cut)
        self.pending = keep
        if not keep:
            self.on_ground.clear()
            self.claimed.clear()  # every later cut sees these gems as already in view

    def _raise_baselines(self, credited: PendingCut | None) -> None:
        for cut in self.pending:
            if cut is not credited and cut.gems_before is not None:
                cut.gems_before += 1


def _map_row(kb: KnowledgeBase, map_id: int) -> dict[str, Any]:
    root = kb.extra.get(KEY)
    if not isinstance(root, dict):
        root = kb.extra[KEY] = {}
    row = root.get(str(map_id))
    if not isinstance(row, dict):
        row = root[str(map_id)] = {}
    if not isinstance(row.get("cuts"), list):
        row["cuts"] = []
    if not isinstance(row.get("regions"), dict):
        row["regions"] = {}
    return row


def record_cut(kb: KnowledgeBase | None, map_id: int, pos: Pos, block: str, tick: int, gem: bool) -> None:
    """File one cut, and count it in its region when it was grass or a bush."""
    if kb is None:
        return
    with kb.lock:
        row = _map_row(kb, map_id)
        row["cuts"].append({"x": pos[0], "y": pos[1], "block": block, "tick": tick, "gem": gem})
        del row["cuts"][:-MAX_RECORDS]
        if block not in GATHER_BLOCKS:
            return
        region = row["regions"].setdefault(region_key(pos), {"cuts": 0, "gems": 0, "last_tick": tick})
        region["cuts"] = int(region.get("cuts", 0)) + 1
        region["gems"] = int(region.get("gems", 0)) + (1 if gem else 0)
        region["last_tick"] = max(int(region.get("last_tick", tick)), tick)


def regions(kb: KnowledgeBase | None, map_id: int | None) -> dict[str, dict[str, int]]:
    """Region totals of one map, ``{"rx,ry": {"cuts", "gems", "last_tick"}}``."""
    if kb is None or map_id is None:
        return {}
    with kb.lock:
        root = kb.extra.get(KEY)
        row = root.get(str(map_id)) if isinstance(root, dict) else None
        raw = row.get("regions") if isinstance(row, dict) else None
        return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}


def barren(region: dict[str, Any]) -> bool:
    return int(region.get("cuts", 0)) >= BARREN_MIN_CUTS and int(region.get("gems", 0)) == 0


def poor(region: dict[str, Any]) -> bool:
    """A fair sample with a low yield (barren ones included)."""
    cuts = int(region.get("cuts", 0))
    return cuts >= FAIR_SAMPLE_CUTS and int(region.get("gems", 0)) < POOR_YIELD * cuts


def good(region: dict[str, Any]) -> bool:
    cuts = int(region.get("cuts", 0))
    return cuts >= GOOD_MIN_CUTS and int(region.get("gems", 0)) >= GOOD_YIELD * cuts and not poor(region)


def poor_regions(kb: KnowledgeBase | None, map_id: int | None) -> set[tuple[int, int]]:
    """The poor regions of one map, as ``(rx, ry)``."""
    return {r for key, v in regions(kb, map_id).items() if poor(v) and (r := _parse(key)) is not None}


def better_region(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> tuple[int, int] | None:
    """The best good region within ``BETTER_REGION_BLOCKS`` of ``pos``, by
    yield then distance, or None."""
    best: tuple[float, int, tuple[int, int]] | None = None
    for key, v in regions(kb, map_id).items():
        r = _parse(key)
        if r is None or not good(v):
            continue
        distance = blocks_to_region(pos, r)
        if distance > BETTER_REGION_BLOCKS:
            continue
        rank = (-int(v.get("gems", 0)) / int(v.get("cuts", 1)), distance, r)
        if best is None or rank < best:
            best = rank
    return best[2] if best is not None else None


def region_corner(region: tuple[int, int]) -> Pos:
    """The block a region is named by: its smallest x, y."""
    return region[0] * REGION_SIZE, region[1] * REGION_SIZE


def barren_regions(kb: KnowledgeBase | None, map_id: int | None) -> set[tuple[int, int]]:
    """The barren regions of one map, as ``(rx, ry)``: read once per decision."""
    if kb is None or map_id is None:
        return set()
    with kb.lock:
        root = kb.extra.get(KEY)
        row = root.get(str(map_id)) if isinstance(root, dict) else None
        raw = row.get("regions") if isinstance(row, dict) else None
        keys = [k for k, v in raw.items() if isinstance(v, dict) and barren(v)] if isinstance(raw, dict) else []
    return {r for r in map(_parse, keys) if r is not None}


def exhausted_cells(
    kb: KnowledgeBase | None, map_id: int | None, tick: int, tracker: GemYieldTracker | None = None
) -> set[Pos]:
    """Cells of one map our own cuts left bare less than ``REGROW_TICKS`` ago.

    A cell is exhausted from the moment of its cut: the ``tracker``'s cuts
    still waiting out their gem window count too, before they are filed, and
    so do cells a ``Use`` left unchanged (:meth:`GemYieldTracker.note_no_effect`).
    """
    pending = tracker.pending_cells(map_id, tick) if tracker is not None else set()
    if kb is None or map_id is None:
        return pending
    with kb.lock:
        root = kb.extra.get(KEY)
        row = root.get(str(map_id)) if isinstance(root, dict) else None
        cuts = row.get("cuts") if isinstance(row, dict) else None
        recent = [c for c in cuts if isinstance(c, dict) and tick - int(c.get("tick", 0)) < REGROW_TICKS] if isinstance(cuts, list) else []
    return pending | {(int(c["x"]), int(c["y"])) for c in recent if "x" in c and "y" in c}


def _parse(key: str) -> tuple[int, int] | None:
    try:
        rx, ry = (int(p) for p in key.split(",", 1))
    except ValueError:
        return None
    return rx, ry


def blocks_to_region(pos: Pos, region: tuple[int, int]) -> int:
    """Chebyshev blocks from ``pos`` to the nearest block of ``region`` (0 inside it)."""
    x0, y0 = region[0] * REGION_SIZE, region[1] * REGION_SIZE
    dx = max(x0 - pos[0], 0, pos[0] - (x0 + REGION_SIZE - 1))
    dy = max(y0 - pos[1], 0, pos[1] - (y0 + REGION_SIZE - 1))
    return max(dx, dy)


def summary(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, Any]:
    """The planner's ``gem_yield``: the best sampled regions, at any distance,
    and the barren ones near us.

    A region is named by its corner block ``x, y`` (``REGION_SIZE`` on a side),
    so a ``gather_gems`` op can name it back; a best region also carries its
    ``distance`` in blocks. ``here`` is the region we stand in, shown only once
    our cuts sampled it: an unsampled region has nothing to name.
    """
    if w.pos is None or w.map_id is None:
        return {}
    here = region_of(w.pos)
    best: list[tuple[float, int, dict[str, Any]]] = []
    dry: list[dict[str, int]] = []
    sampled_here: dict[str, int] | None = None
    for key, region in regions(kb, w.map_id).items():
        r = _parse(key)
        if r is None:
            continue
        cuts, gems = int(region.get("cuts", 0)), int(region.get("gems", 0))
        corner = {"x": r[0] * REGION_SIZE, "y": r[1] * REGION_SIZE}
        if r == here and cuts:
            sampled_here = {**corner, "cuts": cuts, "gems": gems}
        if barren(region):
            if chebyshev(r, here) <= SUMMARY_RADIUS:
                dry.append({**corner, "cuts": cuts})
        elif cuts and gems:
            distance = blocks_to_region(w.pos, r)
            entry = {**corner, "cuts": cuts, "gems": gems, "yield": round(gems / cuts, 2), "distance": distance}
            best.append((gems / cuts, -distance, entry))
    best.sort(key=lambda t: (t[0], t[1]), reverse=True)
    out: dict[str, Any] = {"region_size": REGION_SIZE}
    if sampled_here is not None:
        out["here"] = sampled_here
    out["best"] = [e for _, _, e in best[:SUMMARY_BEST]]
    out["barren"] = sorted(dry, key=lambda d: (d["x"], d["y"]))
    return out
