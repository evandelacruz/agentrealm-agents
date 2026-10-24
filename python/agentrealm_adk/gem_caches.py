"""Gem caches: an oddity as a target worth its cache (A172).

The Manual (Olympuff Gem caches; §11 Breaking blocks, Oddities): each god's
region has one hand-placed oddity, a block that looks out of place, two steps
off the road. Broken with the capability it needs, which may be any of the
five, it drops a gem cache (``gem_cache_5``, ``_7`` or ``_10``) every time,
unless the last cache still lies on its block, and it grows back in 10
minutes.

So an oddity (``oddities``, A134) is a target like any other, worth the cache
it is expected to drop (``expected_cache``): what its breaks have dropped
there, pulled toward the reference's 5, 7 and 10 (``PRIOR_CACHES``). It is
one while a capability held may break it (``capabilities_for``) and it is
there to break (``available``): its regrow timer from the last break that
opened it has run (``back_at_tick``), it shows on its cell when that cell is
in sight, and no cache lies on it. One no held capability may break is no
target; the planner is told what it needs (``needs_lines``).

What a break dropped is filed by cell in the knowledge base
(``note_caches``), a break of ours that dropped nothing as a 0, so a lone
block that is only odd is soon priced near nothing::

    "gem_caches": {"<map_id>,<x>,<y>": {"drops": [7, 0], "last_id": <supply id>, "settled": <tick>}}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .break_memory import FAILED_RESULTS, break_key, held_capabilities, pick_supply_for_capability
from .gem_yield import GEM_CACHE_PREFIX
from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase
from .oddities import ODDITIES_SHOWN, back_at_tick, find, remember, remembered
from .supplies import CAPABILITIES
from .world import Entity, Pos, WorldModel, chebyshev

CACHES_KEY = "gem_caches"
PRIOR_CACHES = (5, 7, 10)  # what a region's cache may be worth, by ring (Manual Gem caches)
DROPS_KEPT = 10  # drops remembered per oddity, the latest kept
CACHE_SEEN_TICKS = 20  # a break's cell this long in sight with no cache on it: it dropped none


@dataclass(frozen=True)
class CacheTarget:
    """An oddity to break for its cache: where, its block, the capability and
    held supply to try, and the gems it is expected to drop."""

    pos: Pos
    block: str
    capability: str
    supply: InventorySupply
    gems: float


def cache_gems(code: str | None) -> int:
    """The gems a cache's code names (``gem_cache_7`` is 7); 0 for anything else."""
    if not code or not code.startswith(GEM_CACHE_PREFIX):
        return 0
    n = code.removeprefix(GEM_CACHE_PREFIX).strip("_")
    return int(n) if n.isdigit() else 0


def caches_on(w: WorldModel, pos: Pos) -> list[Entity]:
    """The gem caches in sight lying on ``pos``."""
    return [e for e in w.entities if e.kind == "supply" and e.pos == pos and cache_gems(e.code)]


def oddities_here(w: WorldModel, kb: KnowledgeBase | None) -> dict[Pos, str]:
    """Every oddity on this map, found now or remembered, filed first (``oddities.remember``)."""
    found = find(w)
    remember(w, kb, found)
    return {**remembered(w, kb), **found}


def note_caches(w: WorldModel, kb: KnowledgeBase | None) -> None:
    """File what each break of a remembered oddity dropped (A172): a gem
    cache in sight lying on it, once per cache (its supply id); and, for a
    break of ours that opened it (break memory's ``opened``), a zero once its
    cell has been in sight for ``CACHE_SEEN_TICKS`` after the break with no
    cache on it, so a lone block that is only odd stops being priced at the
    prior. Each opening is settled once (``settled``, its tick)."""
    if kb is None or w.map_id is None:
        return
    for pos in remembered(w, kb):
        opened = latest_opening(kb, w.map_id, pos)
        cache = next(iter(caches_on(w, pos)), None)
        in_sight = w.pos is not None and chebyshev(w.pos, pos) <= w.perception
        with kb.lock:
            rows = kb.extra.setdefault(CACHES_KEY, {})
            key = f"{w.map_id},{pos[0]},{pos[1]}"
            row = rows.get(key) or {}
            unsettled = opened is not None and row.get("settled") != opened
            if cache is not None and row.get("last_id") != cache.id:
                drop = cache_gems(cache.code)
                row["last_id"] = cache.id
            elif cache is None and unsettled and in_sight and w.tick >= opened + CACHE_SEEN_TICKS:
                drop = 0
            else:
                continue
            row["drops"] = (list(row.get("drops") or []) + [drop])[-DROPS_KEPT:]
            if opened is not None:
                row["settled"] = opened
            rows[key] = row


def drops(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> list[int]:
    """What breaks of the oddity at ``pos`` have dropped, oldest first; a break that dropped nothing is a 0."""
    if kb is None or map_id is None:
        return []
    with kb.lock:
        row = (kb.extra.get(CACHES_KEY) or {}).get(f"{map_id},{pos[0]},{pos[1]}")
        seen = list(row.get("drops") or []) if isinstance(row, dict) else []
    return [int(n) for n in seen if isinstance(n, int) and not isinstance(n, bool) and n >= 0]


def expected_cache(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> float:
    """The gems the oddity at ``pos`` is expected to drop: the mean of its
    drops with the reference's mean (``PRIOR_CACHES``) counted as one more."""
    seen = drops(kb, map_id, pos)
    prior = sum(PRIOR_CACHES) / len(PRIOR_CACHES)
    return (sum(seen) + prior) / (len(seen) + 1)


def _tries(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> dict[str, dict[str, Any]]:
    if kb is None or map_id is None:
        return {}
    with kb.lock:
        rows = {c: kb.breaks.get(break_key(map_id, pos, c)) for c in sorted(CAPABILITIES)}
    return {c: row for c, row in rows.items() if isinstance(row, dict)}


def latest_opening(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> int | None:
    """The tick of the latest break that opened ``pos``; None when none has."""
    ticks = [int(row.get("tick") or 0) for row in _tries(kb, map_id, pos).values() if row.get("result") == "opened"]
    return max(ticks) if ticks else None


def opener(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> str | None:
    """The capability a break of ``pos`` opened it with, once one has."""
    return next((c for c, row in _tries(kb, map_id, pos).items() if row.get("result") == "opened"), None)


def capabilities_for(w: WorldModel, kb: KnowledgeBase | None, pos: Pos) -> list[str]:
    """The capabilities that may break the oddity at ``pos``: the one that
    opened it before, else every one that has not failed there (an odd block
    may need any of the five, A160)."""
    known = opener(kb, w.map_id, pos)
    if known is not None:
        return [known]
    failed = {c for c, row in _tries(kb, w.map_id, pos).items() if row.get("result") in FAILED_RESULTS}
    return [c for c in sorted(CAPABILITIES) if c not in failed]


def available(w: WorldModel, kb: KnowledgeBase | None, pos: Pos, block: str, tick_hz: int) -> bool:
    """Whether the oddity at ``pos`` is there to break: its regrow timer has
    run, no cache lies on it, and, with its cell in sight, it shows."""
    back = back_at_tick(kb, w.map_id, pos, tick_hz)
    if back is not None and w.tick < back:
        return False
    if caches_on(w, pos):
        return False
    in_sight = w.pos is not None and chebyshev(w.pos, pos) <= w.perception
    return not in_sight or w.view.tiles.get(pos) == block


def cache_targets(w: WorldModel, kb: KnowledgeBase | None, tick_hz: int) -> list[CacheTarget]:
    """Every oddity on this map she may break now for its cache: one a held
    capability may break (``capabilities_for``), with the supply to use
    (``break_memory.pick_supply_for_capability``), that is ``available``."""
    held = held_capabilities(w, kb)
    out: list[CacheTarget] = []
    for pos, block in sorted(oddities_here(w, kb).items()):
        caps = [c for c in capabilities_for(w, kb, pos) if c in held]
        if not caps or not available(w, kb, pos, block, tick_hz):
            continue
        supply = pick_supply_for_capability(w, caps[0], kb)
        if supply is not None:
            out.append(CacheTarget(pos, block, caps[0], supply, expected_cache(kb, w.map_id, pos)))
    return out


def needs_lines(w: WorldModel, kb: KnowledgeBase | None) -> list[str]:
    """Each oddity remembered on this map that no held capability may break,
    with what it needs: the capability that opened it, else those not yet
    failed there; the ``ODDITIES_SHOWN`` nearest, as Gary's State shows
    oddities. Never walked to; the planner is told (A172)."""
    held = held_capabilities(w, kb)
    here = w.pos or (0, 0)
    out = []
    for pos, block in sorted(remembered(w, kb).items(), key=lambda pb: (chebyshev(here, pb[0]), pb[0])):
        caps = capabilities_for(w, kb, pos)
        if any(c in held for c in caps):
            continue
        if len(out) == ODDITIES_SHOWN:
            break
        need = caps[0] if len(caps) == 1 else f"one of {', '.join(caps)}" if caps else "a capability none has opened it with"
        out.append(f"{block} at {pos[0]},{pos[1]} worth about {expected_cache(kb, w.map_id, pos):.0f} gems needs {need}")
    return out
