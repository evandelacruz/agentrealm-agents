"""Oddities: blocks that look out of place, shown to the planner (A134).

The Manual: each god's region hides one hand-placed oddity, a block of a type
that does not belong where it stands (one rock in a garden, one bush in a
wheat field, one tree in a maze). Broken with the capability it needs, which
may be any of the five whatever its type, it drops a gem cache every time,
unless the last cache still lies on its block, and it grows back (Manual §11
Regrowth, drops, and returns). More generally a block type alone where it
does not belong is often destroyable and worth checking. So the agent points
the planner at such blocks; whether one is worth the walk and a tool is the
planner's call, and the ``break_block`` op does the work (A87).

The rule, on the terrain the agent remembers of the map (``MapView.tiles``):
a block is odd when

- its type is breakable (``break_memory.BREAKABLE``);
- no other cell of its 7x7 neighbourhood (``NEIGHBOURHOOD`` each way) is of
  its type, of at least ``MIN_SEEN`` neighbours seen;
- one other type makes up at least ``DOMINANT_SHARE`` of the neighbours seen.

So a lone rock in grass is odd and a field of rocks is not. It over-reports on
purpose: a false one costs a walk and one try, and break memory keeps the try.

Each one found is remembered in the world's knowledge base, since a broken one
no longer shows as a block, and judged again on every look: one whose block
still shows but no longer passes the rule is dropped::

    "oddities": {"<map_id>,<x>,<y>": {"block": "rock", "seen": <tick>}}

Its tries are break memory's rows for its cell (``break_memory.break_key``):
the last one, with its result and tick, shows in State; a capability that
failed there is not offered again; and one opened shows when it is due back.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .break_memory import BLOCK_OPENERS, BREAKABLE, FAILED_RESULTS, break_key, held_capabilities
from .knowledge_base import KnowledgeBase
from .supplies import CAPABILITIES
from .world import Pos, WorldModel, chebyshev

ODDITIES_KEY = "oddities"
NEIGHBOURHOOD = 3  # cells each way: the 7x7 square round a block
MIN_SEEN = 24  # neighbours seen, of 48, before a block is judged
DOMINANT_SHARE = 0.5  # share of the seen neighbours one other type must hold
SCAN_RADIUS = 15  # blocks judged this far round the character: the State map
ODDITIES_SHOWN = 5  # State lists the nearest few
ODDITIES_KEPT = 200  # remembered per world, the latest seen kept
RETURN_SECONDS = 600  # a broken oddity grows back in 10 minutes in Olympuff (Manual §11 Regrowth)


def is_odd(tiles: Any, pos: Pos) -> bool:
    """The rule above, for the block at ``pos`` in ``tiles`` (cell -> block type)."""
    block = tiles.get(pos)
    if block not in BREAKABLE:
        return False
    x, y = pos
    seen: Counter[str] = Counter()
    for dx in range(-NEIGHBOURHOOD, NEIGHBOURHOOD + 1):
        for dy in range(-NEIGHBOURHOOD, NEIGHBOURHOOD + 1):
            other = tiles.get((x + dx, y + dy)) if (dx, dy) != (0, 0) else None
            if other is not None:
                seen[other] += 1
    total = sum(seen.values())
    if total < MIN_SEEN or seen[block]:
        return False
    return seen.most_common(1)[0][1] >= DOMINANT_SHARE * total


def find(w: WorldModel) -> dict[Pos, str]:
    """Odd blocks within ``SCAN_RADIUS`` of the character, by cell."""
    if w.pos is None:
        return {}
    cx, cy = w.pos
    tiles = w.view.tiles
    return {
        (x, y): tiles[(x, y)]
        for x in range(cx - SCAN_RADIUS, cx + SCAN_RADIUS + 1)
        for y in range(cy - SCAN_RADIUS, cy + SCAN_RADIUS + 1)
        if is_odd(tiles, (x, y))
    }


def remember(w: WorldModel, kb: KnowledgeBase | None, found: dict[Pos, str]) -> None:
    """File the oddities ``found`` on this map in ``kb``, keeping the latest
    ``ODDITIES_KEPT``, and judge the remembered ones again: one whose cell
    still shows its block but no longer passes :func:`is_odd` (more of the
    ground round it seen since) is dropped. One whose block is gone stays, so
    a broken one shows when it is back."""
    if kb is None or w.map_id is None:
        return
    tiles = w.view.tiles
    with kb.lock:
        rows = kb.extra.setdefault(ODDITIES_KEY, {})
        for pos, block in remembered_rows(rows, w.map_id).items():
            if tiles.get(pos) == block and not is_odd(tiles, pos):
                del rows[f"{w.map_id},{pos[0]},{pos[1]}"]
        for (x, y), block in found.items():
            rows[f"{w.map_id},{x},{y}"] = {"block": block, "seen": w.tick}
        if len(rows) > ODDITIES_KEPT:
            for key in sorted(rows, key=lambda k: rows[k].get("seen", 0))[: len(rows) - ODDITIES_KEPT]:
                del rows[key]


def remembered(w: WorldModel, kb: KnowledgeBase | None) -> dict[Pos, str]:
    """The oddities ``kb`` remembers on this map, by cell."""
    if kb is None or w.map_id is None:
        return {}
    with kb.lock:
        return remembered_rows(kb.extra.get(ODDITIES_KEY) or {}, w.map_id)


def remembered_rows(rows: dict[str, Any], map_id: int) -> dict[Pos, str]:
    """The block of each row in ``rows`` on ``map_id``, by cell; a row that does not parse is skipped."""
    out: dict[Pos, str] = {}
    for key, row in rows.items():
        try:
            row_map, x, y = (int(p) for p in key.split(","))
        except ValueError:
            continue
        if row_map == map_id and isinstance(row, dict) and row.get("block") in BREAKABLE:
            out[(x, y)] = row["block"]
    return out


def oddity_row(w: WorldModel, kb: KnowledgeBase | None, pos: Pos, block: str, tick_hz: int) -> dict[str, Any]:
    """One oddity as State shows it: cell and distance, block, ``may_break_with``
    (each held capability known to open its type, or that opened it before,
    and that has not failed there; else ``none held``), ``failed``
    capabilities, its ``last_try`` and, once opened, ``back_at_tick``."""
    tries = _tries(kb, w.map_id, pos)
    failed = sorted(c for c, result, _ in tries if result in FAILED_RESULTS)
    openers = BLOCK_OPENERS[block] | {c for c, result, _ in tries if result == "opened"}
    usable = sorted(c for c in openers & held_capabilities(w, kb) if c not in failed)
    row: dict[str, Any] = {
        "x": pos[0],
        "y": pos[1],
        "cells": chebyshev(w.pos, pos) if w.pos else None,
        "block": block,
        "may_break_with": usable or "none held",
    }
    if failed:
        row["failed"] = failed
    if tries:
        cap, result, tick = max(tries, key=lambda t: t[2])
        row["last_try"] = {"capability": cap, "result": result, "tick": tick}
        if result == "opened":
            row["back_at_tick"] = tick + RETURN_SECONDS * tick_hz
    return row


def _tries(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> list[tuple[str, str, int]]:
    """Each capability break memory recorded at ``pos``, with its result and tick."""
    if kb is None or map_id is None:
        return []
    with kb.lock:
        rows = {c: kb.breaks.get(break_key(map_id, pos, c)) for c in sorted(CAPABILITIES)}
    return [
        (c, str(row.get("result")), int(row.get("tick") or 0))
        for c, row in rows.items()
        if isinstance(row, dict)
    ]


def oddities_line(w: WorldModel, kb: KnowledgeBase | None, tick_hz: int) -> str:
    """State ``oddities`` (A134): the ``ODDITIES_SHOWN`` nearest oddities on this
    map, found now or remembered (filed first, :func:`remember`), each as
    :func:`oddity_row` shows it."""
    found = find(w)
    remember(w, kb, found)
    cells = {**remembered(w, kb), **found}
    if not cells or w.pos is None:
        return "oddities=none seen"
    near = sorted(cells, key=lambda p: (chebyshev(w.pos, p), p))
    rows = [oddity_row(w, kb, p, cells[p], tick_hz) for p in near[:ODDITIES_SHOWN]]
    more = f" (+{len(near) - len(rows)} more)" if len(near) > len(rows) else ""
    return f"oddities={json.dumps(rows, sort_keys=True)}{more}"
