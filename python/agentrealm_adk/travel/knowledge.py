"""Travel-related knowledge-base sections (A27)."""

from __future__ import annotations

from typing import Any

from ..knowledge_base import KnowledgeBase
from ..knowledge_maps import HUNTING
from ..world import Pos

SHOPS = "shops"
NOT_SOLD = "not_sold"
TOWN = "town"
SOLD_OUT = "sold_out"

# Shop stock returns on its home block this long after it is bought (Manual
# §11 Regrowth, drops, and returns, A159): a shop cell seen without its item is
# sold out until then, not forgotten.
RESTOCK_SECONDS = 120
# A shop cell seen without its item, never hidden by another supply, for this
# long since it was first seen so has lost the sale: stock that returns would
# have shown by now (A159).
SALE_LAPSE_SECONDS = 600


def _cell_key(p: Pos) -> str:
    return f"{p[0]},{p[1]}"


def sync_town(kb: KnowledgeBase, town: dict | None) -> None:
    if town is None:
        return
    try:
        entry = {
            "map_id": int(town["map_id"]),
            "x": int(town["x"]),
            "y": int(town["y"]),
        }
    except (KeyError, TypeError, ValueError):
        return
    with kb.lock:
        kb.extra[TOWN] = entry


def town_from_kb(kb: KnowledgeBase | None) -> tuple[int, Pos] | None:
    if kb is None:
        return None
    with kb.lock:
        raw = kb.extra.get(TOWN)
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw["map_id"]), (int(raw["x"]), int(raw["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def entrance_key(map_id: int, p: Pos) -> str:
    """``kb.entrances`` key: map and cell, so the same x,y on two maps stay apart."""
    return f"{map_id}:{p[0]},{p[1]}"


def iter_entrances(kb: KnowledgeBase | None) -> list[tuple[int, Pos, dict[str, Any]]]:
    """Every entrance row as ``(map_id, pos, row)``; the row is the live dict."""
    if kb is None:
        return []
    out: list[tuple[int, Pos, dict[str, Any]]] = []
    with kb.lock:
        for key, row in kb.entrances.items():
            if not isinstance(row, dict):
                continue
            try:
                mid, cell = key.split(":", 1)
                x, y = (int(p) for p in cell.split(",", 1))
                out.append((int(mid), (x, y), row))
            except ValueError:
                continue
    return out


def sync_entrances(kb: KnowledgeBase, minimap: dict) -> None:
    """Merge minimap entrance marks into ``kb.entrances`` (Manual minimap)."""
    with kb.lock:
        for m in minimap.get("maps") or []:
            if not isinstance(m, dict):
                continue
            try:
                map_id = int(m["map_id"])
            except (KeyError, TypeError, ValueError):
                continue
            for ent in m.get("entrances") or []:
                if not isinstance(ent, dict):
                    continue
                try:
                    x, y = int(ent["x"]), int(ent["y"])
                except (KeyError, TypeError, ValueError):
                    continue
                row = kb.entrances.setdefault(entrance_key(map_id, (x, y)), {})
                row["map_id"] = map_id
                row.setdefault("x", x)
                row.setdefault("y", y)


def iter_hunting_cells(kb: KnowledgeBase | None) -> list[tuple[int, Pos, dict[str, Any]]]:
    if kb is None:
        return []
    out: list[tuple[int, Pos, dict[str, Any]]] = []
    with kb.lock:
        for mid, entry in kb.maps.items():
            if not isinstance(entry, dict):
                continue
            try:
                map_id = int(mid)
            except ValueError:
                continue
            hunting = entry.get(HUNTING)
            if not isinstance(hunting, dict):
                continue
            for key, fact in hunting.items():
                # A closed cell stays a candidate while its ceiling is known: the
                # strength bracket rules it out, and a loadout change reopens it.
                if not isinstance(fact, dict) or (fact.get("closed") and "strength_ceiling" not in fact):
                    continue
                try:
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                out.append((map_id, (x, y), fact))
    return out


def record_shop_cell(
    kb: KnowledgeBase | None, map_id: int, pos: Pos, code: str | None = None, price: int | None = None
) -> None:
    """A shop cell, and what it was last seen selling there (A131): ``sells``
    maps the item seen for sale on the cell to its gem price. A cell holds one
    supply, so a new sighting replaces what it sold before, sold out or not
    (A159). A priced supply our own ``Drop`` put down is no shop's: the
    callers leave it out (``WorldModel.is_our_drop``, A158)."""
    if kb is None:
        return
    with kb.lock:
        shops = kb.maps.setdefault(str(map_id), {}).setdefault(SHOPS, {})
        row = shops.get(_cell_key(pos))
        if not isinstance(row, dict):
            row = shops[_cell_key(pos)] = {}
        if code:
            row["sells"] = {code: price}
            row.pop(SOLD_OUT, None)
            _lapse_locked(kb, {code})


def note_sold_out(
    kb: KnowledgeBase | None, map_id: int, pos: Pos, code: str, tick: int, tick_hz: int, *, hidden: bool = False
) -> None:
    """The shop cell was seen at ``tick`` without ``code`` for sale (A159).

    Its stock returns ``RESTOCK_SECONDS`` after it is bought, so the cell is
    sold out until then (``back``, a tick), not forgotten. ``since`` is the
    tick it was first seen so; once that absence runs to
    ``SALE_LAPSE_SECONDS`` the sale lapses and the cell sells it no more.
    ``hidden``: another supply lies on the cell, which hides the stock, so
    the absence starts again. A cell saved before A131 (no ``sells``) is
    checked: it sells nothing known."""
    if kb is None:
        return
    with kb.lock:
        row = kb.maps.get(str(map_id), {}).get(SHOPS, {}).get(_cell_key(pos))
        if not isinstance(row, dict):
            return
        sells = row.get("sells")
        if not isinstance(sells, dict):
            row["sells"] = {}
            return
        if code not in sells:
            return
        out = row.setdefault(SOLD_OUT, {})
        prior = out.get(code)
        since = prior.get("since") if isinstance(prior, dict) and not hidden else None
        since = since if isinstance(since, int) and since <= tick else tick
        if tick - since >= SALE_LAPSE_SECONDS * tick_hz:
            del sells[code]
            out.pop(code, None)
        else:
            out[code] = {"since": since, "back": tick + RESTOCK_SECONDS * tick_hz}
        if not out:
            del row[SOLD_OUT]


def iter_sold_out(kb: KnowledgeBase | None) -> dict[tuple[int, Pos, str], int]:
    """Each shop cell's sold-out item: (map, cell, code) -> the tick its stock is back (A159)."""
    if kb is None:
        return {}
    out: dict[tuple[int, Pos, str], int] = {}
    with kb.lock:
        for mid, entry in kb.maps.items():
            shops = entry.get(SHOPS) if isinstance(entry, dict) else None
            if not isinstance(shops, dict):
                continue
            for key, row in shops.items():
                rows = row.get(SOLD_OUT) if isinstance(row, dict) else None
                if not isinstance(rows, dict):
                    continue
                try:
                    map_id = int(mid)
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                for code, fact in rows.items():
                    back = fact.get("back") if isinstance(fact, dict) else None
                    if isinstance(back, int):
                        out[(map_id, (x, y), code)] = back
    return out


def note_not_sold(kb: KnowledgeBase | None, map_id: int, pos: Pos, code: str, tick: int) -> None:
    """A seller's place (a clue's cell, a place a buy named) was searched round
    without ``code`` for sale, at ``tick`` (A131): it is no seller of it until
    newer evidence, a clue found after then or a shop seen selling it."""
    if kb is None:
        return
    with kb.lock:
        rows = kb.maps.setdefault(str(map_id), {}).setdefault(NOT_SOLD, {})
        rows.setdefault(_cell_key(pos), {})[code] = tick


def lapse_not_sold(kb: KnowledgeBase | None, codes: set[str]) -> None:
    """Newer evidence ``codes`` are sold (a clue offering one, a shop seen
    selling one): every place searched without them is a seller again (A131)."""
    if kb is None or not codes:
        return
    with kb.lock:
        _lapse_locked(kb, codes)


def _lapse_locked(kb: KnowledgeBase, codes: set[str]) -> None:
    for entry in kb.maps.values():
        rows = entry.get(NOT_SOLD) if isinstance(entry, dict) else None
        if not isinstance(rows, dict):
            continue
        for key in list(rows):
            left = {c: t for c, t in rows[key].items() if c not in codes} if isinstance(rows[key], dict) else {}
            if left:
                rows[key] = left
            else:
                del rows[key]


def iter_not_sold(kb: KnowledgeBase | None) -> dict[tuple[int, Pos, str], int]:
    """Every place searched without an item for sale: (map, cell, code) -> tick."""
    if kb is None:
        return {}
    out: dict[tuple[int, Pos, str], int] = {}
    with kb.lock:
        for mid, entry in kb.maps.items():
            rows = entry.get(NOT_SOLD) if isinstance(entry, dict) else None
            if not isinstance(rows, dict):
                continue
            for key, codes in rows.items():
                try:
                    map_id = int(mid)
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                for code, tick in (codes.items() if isinstance(codes, dict) else ()):
                    if isinstance(tick, int):
                        out[(map_id, (x, y), code)] = tick
    return out


def iter_unchecked_shop_cells(kb: KnowledgeBase | None) -> list[tuple[int, Pos]]:
    """Shop cells saved before A131, with no record of what they sell: each
    may sell any item a price was seen for, until it is seen again."""
    if kb is None:
        return []
    out: list[tuple[int, Pos]] = []
    with kb.lock:
        for mid, entry in kb.maps.items():
            shops = entry.get(SHOPS) if isinstance(entry, dict) else None
            if not isinstance(shops, dict):
                continue
            for key, row in shops.items():
                if isinstance(row, dict) and "sells" in row:
                    continue
                try:
                    map_id = int(mid)
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                out.append((map_id, (x, y)))
    return out


def iter_shop_sales(kb: KnowledgeBase | None) -> list[tuple[int, Pos, str, int | None]]:
    """Each item a known shop cell was seen selling: map, cell, code and price (A131)."""
    if kb is None:
        return []
    out: list[tuple[int, Pos, str, int | None]] = []
    with kb.lock:
        for mid, entry in kb.maps.items():
            shops = entry.get(SHOPS) if isinstance(entry, dict) else None
            if not isinstance(shops, dict):
                continue
            for key, row in shops.items():
                sells = row.get("sells") if isinstance(row, dict) else None
                if not isinstance(sells, dict):
                    continue
                try:
                    map_id = int(mid)
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                for code, price in sells.items():
                    out.append((map_id, (x, y), code, price if isinstance(price, int) else None))
    return out


def iter_shop_cells(kb: KnowledgeBase | None) -> list[tuple[int, Pos]]:
    if kb is None:
        return []
    out: list[tuple[int, Pos]] = []
    with kb.lock:
        for mid, entry in kb.maps.items():
            if not isinstance(entry, dict):
                continue
            try:
                map_id = int(mid)
            except ValueError:
                continue
            shops = entry.get(SHOPS)
            if not isinstance(shops, dict):
                continue
            for key in shops:
                try:
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                out.append((map_id, (x, y)))
    return out


def entrance_from_kb(kb: KnowledgeBase | None, map_id: int | None, x: int, y: int) -> tuple[int, Pos] | None:
    if map_id is not None:
        return map_id, (x, y)
    # No map given: take the one mark at x,y, if only one map has a mark there.
    maps = {mid for mid, pos, _ in iter_entrances(kb) if pos == (x, y)}
    if len(maps) == 1:
        return maps.pop(), (x, y)
    return None
