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
regions (:func:`barren_regions`, read once per decision).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .break_memory import BREAKABLE
from .knowledge_base import KnowledgeBase
from .world import Pos, WorldModel, chebyshev

KEY = "gem_yield"
REGION_SIZE = 16  # blocks per region side
# A fair sample: this many cuts with no gem marks a region barren. At the
# manual's lowest rate (10% a cut, GAME_NOTES.md Gems) a normal region shows
# no gem in 30 cuts about 4% of the time (0.9**30); at 15 it would be 21%.
BARREN_MIN_CUTS = 30
GEM_WINDOW_TICKS = 5  # a gem that shows up later than this is not the cut's
MAX_RECORDS = 500  # cut records kept per map; region totals are kept in full
CUT_BLOCKS = BREAKABLE | {"grass"}  # blocks a cut or break is recorded on
GATHER_BLOCKS = frozenset({"grass", "bush"})  # the cuts the region yield counts
SUMMARY_RADIUS = 3  # regions this far (Chebyshev, in regions) from ours are "nearby"
SUMMARY_BEST = 3  # best regions shown to the planner
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

    def note_cut(self, w: WorldModel, pos: Pos, block: str, tick: int, *, took: bool = False) -> None:
        """Our ``Use`` on ``pos`` applied while it showed ``block``. ``took``: a
        ``Take`` applied earlier in the same response, whose gem the counter
        does not show yet."""
        if w.map_id is None or not block:
            return
        self.pending.append(
            PendingCut(w.map_id, pos, block, tick, w.gems, {gid for gid, _ in _ground_gems(w)}, took=took)
        )

    def note_take(self) -> None:
        for cut in self.pending:
            cut.took = True

    def update(self, w: WorldModel, kb: KnowledgeBase | None) -> None:
        """After a round trip is applied: credit gems, file cuts whose window closed."""
        rise = 0 if self.last_gems is None or w.gems is None else w.gems - self.last_gems
        self.last_gems = w.gems
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


def _parse(key: str) -> tuple[int, int] | None:
    try:
        rx, ry = (int(p) for p in key.split(",", 1))
    except ValueError:
        return None
    return rx, ry


def summary(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, Any]:
    """The planner's ``gem_yield``: the best sampled regions near us and the barren ones.

    A region is named by its corner block ``x, y`` (``REGION_SIZE`` on a side),
    so a ``gather_gems`` op can name it back.
    """
    if w.pos is None or w.map_id is None:
        return {}
    here = region_of(w.pos)
    best: list[tuple[float, int, dict[str, Any]]] = []
    dry: list[dict[str, int]] = []
    for key, region in regions(kb, w.map_id).items():
        r = _parse(key)
        if r is None or chebyshev(r, here) > SUMMARY_RADIUS:
            continue
        cuts, gems = int(region.get("cuts", 0)), int(region.get("gems", 0))
        corner = {"x": r[0] * REGION_SIZE, "y": r[1] * REGION_SIZE}
        if barren(region):
            dry.append({**corner, "cuts": cuts})
        elif cuts and gems:
            entry = {**corner, "cuts": cuts, "gems": gems, "yield": round(gems / cuts, 2)}
            best.append((gems / cuts, -chebyshev(r, here), entry))
    best.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return {
        "region_size": REGION_SIZE,
        "here": {"x": here[0] * REGION_SIZE, "y": here[1] * REGION_SIZE},
        "best": [e for _, _, e in best[:SUMMARY_BEST]],
        "barren": sorted(dry, key=lambda d: (d["x"], d["y"])),
    }
