"""Who sells an item, and where (A131).

A ``buy`` walks only toward a seller known to sell its item, never to a shop
that does not. ``sellers_of`` is the one seller test. A seller is known by:

- ``shop``: a shop cell seen selling the item (``travel.knowledge.iter_shop_sales``),
  with its gem price. The cell itself is where to go.
- ``clue``: a sign, scroll or NPC line with a phrase that names the item and
  a gem price ("Raft, thirty gems"), at the cell the clue was found
  (``clues``). The seller is somewhere round it: Shop walks there and searches.
- ``plan``: the place the ``buy`` op names (``x``, ``y``, ``map_id``), the
  planner's reading of where a seller is, such as a town a clue names.
  Searched round the same way.
- ``unchecked``: a shop cell saved before A131, with no record of what it
  sells, for an item a price was seen for. It is taken only after every
  other seller, and checked off once seen (``forget_sale``).

A price seen with no place is no seller. Of several, the cheapest walk is
taken. With none known the buy is refused when a reply puts it on the stack
(``refuse_unsold``) and dropped by Shop, never sent on a walk. State
``no_seller_known`` (A110) and ``sellers`` read the same test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from .healing import supply_matches
from .knowledge_base import KnowledgeBase, knowledge_items
from .travel.knowledge import iter_shop_sales, iter_unchecked_shop_cells
from .world import Pos, WorldModel, chebyshev

# How far round a seller's place (a clue's cell, a place the op names) Shop searches for it.
SELLER_SEARCH_RADIUS = 20
# The nearest sellers State shows per item, and the most items it shows
# beside the ones the stack buys, nearest seller first.
SELLERS_SHOWN = 3
SELLER_ITEMS_SHOWN = 8

_GEMS = re.compile(r"\bgems?\b")
# A clue's phrases: an item and its price are named in the same one.
_PHRASE = re.compile(r"[.!?;\n]+")


@dataclass(frozen=True)
class Seller:
    """Where an item is sold: the cell itself (``exact``), or a place to search round."""

    map_id: int
    pos: Pos
    by: str  # "shop", "clue", "plan" or "unchecked"
    exact: bool
    price: int | None = None


@dataclass
class _Known:
    """What the knowledge base says of sellers, read once for many items."""

    sales: list[tuple[int, Pos, str, int | None]]
    clues: list[dict[str, Any]]
    unchecked: list[tuple[int, Pos]]
    priced: dict[str, int]


def _known(kb: KnowledgeBase | None) -> _Known:
    if kb is None:
        return _Known([], [], [], {})
    with kb.lock:
        clues = list(kb.clues)
    priced = {
        code: row["gem_price"]
        for code, row in knowledge_items(kb).items()
        if isinstance(row, dict) and isinstance(row.get("gem_price"), int) and row["gem_price"] > 0
    }
    return _Known(iter_shop_sales(kb), clues, iter_unchecked_shop_cells(kb), priced)


def _names(code: str, text: str) -> bool:
    """The text names the item: its code's words, in any case, plural allowed."""
    words = [re.escape(p) for p in code.lower().split("_") if p]
    return bool(words) and re.search(r"\b" + r"[\s_-]+".join(words) + r"(?:e?s)?\b", text) is not None


def _offers(code: str, text: str) -> bool:
    """One phrase of the text names the item and a gem price."""
    return any(_GEMS.search(p) and _names(code, p) for p in _PHRASE.split(text.lower()))


def _clue_sellers(code: str, rows: list[dict[str, Any]]) -> list[Seller]:
    out = []
    for row in rows:
        text = row.get("text")
        if not isinstance(text, str) or not _offers(code, text):
            continue
        try:
            out.append(Seller(int(row["map_id"]), (int(row["x"]), int(row["y"])), "clue", False))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _op_seller(op: dict[str, Any] | None) -> Seller | None:
    """The place the op names; ``_validate_buy`` takes it only with its map."""
    if not op or not all(k in op for k in ("x", "y", "map_id")):
        return None
    return Seller(int(op["map_id"]), (int(op["x"]), int(op["y"])), "plan", False)


def _sellers(code: str, w: WorldModel, known: _Known, op: dict[str, Any] | None = None) -> list[Seller]:
    found: list[Seller] = []
    if (named := _op_seller(op)) is not None:
        found.append(named)
    found += [Seller(mid, pos, "shop", True, price) for mid, pos, sold, price in known.sales if supply_matches(code, sold)]
    found += _clue_sellers(code, known.clues)
    price = next((p for c, p in known.priced.items() if supply_matches(code, c)), None)
    if price is not None:
        found += [Seller(mid, pos, "unchecked", True) for mid, pos in known.unchecked]
    return sorted(dict.fromkeys(found), key=lambda s: walk_order(s, w))


def sellers_of(code: str, w: WorldModel, kb: KnowledgeBase | None, op: dict[str, Any] | None = None) -> list[Seller]:
    """Every seller of ``code`` known, cheapest walk first, an unchecked shop last."""
    return _sellers(code, w, _known(kb), op)


def walk_estimate(s: Seller, w: WorldModel) -> int | None:
    """Roughly how many steps the walk to ``s`` is; None on another map."""
    if w.pos is None or s.map_id != w.map_id:
        return None
    return chebyshev(w.pos, s.pos)


def walk_order(s: Seller, w: WorldModel) -> tuple:
    """Cheapest walk first: this map before another, then fewer steps; a
    shop not known to sell the item only after every one that is."""
    steps = walk_estimate(s, w)
    return (s.by == "unchecked", steps is None, steps or 0, s.map_id, s.pos, s.by)


def for_sale_in_sight(code: str, w: WorldModel) -> bool:
    return any(e.kind == "supply" and isinstance(e.gem_price, int) and e.gem_price > 0 and supply_matches(code, e.code) for e in w.entities)


def seller_known(code: str, w: WorldModel, kb: KnowledgeBase | None, op: dict[str, Any] | None = None) -> bool:
    """A buy of ``code`` has somewhere to go: a seller known, or the item for sale in sight."""
    return for_sale_in_sight(code, w) or bool(sellers_of(code, w, kb, op))


def refuse_unsold(
    goals: list[dict[str, Any]], w: WorldModel, kb: KnowledgeBase | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ``buy`` ops with no seller known are left out, each with why (A110, A131)."""
    kept, refused = [], []
    for g in goals:
        if g.get("op") == "buy" and not seller_known(g["code"], w, kb, g):
            refused.append(
                f"{json.dumps(g, sort_keys=True)} left out: no seller of {g['code']} is known (no shop seen selling it, "
                "no clue naming it with a gem price); find one first, or give x, y and map_id for the place a clue says sells it"
            )
            continue
        kept.append(g)
    return kept, refused


def sellers_line(w: WorldModel, kb: KnowledgeBase | None, wanted: Collection[str] = ()) -> str:
    """Where each item is sold, nearest first with roughly how far (A131):
    each item the plan wants to buy (``none known`` when it has none), then
    up to ``SELLER_ITEMS_SHOWN`` more a shop is known to sell, nearest first."""
    known = _known(kb)
    rows: dict[str, Any] = {}
    for code in sorted(set(wanted)):
        found = _sellers(code, w, known)
        rows[code] = [_row(s, w) for s in found[:SELLERS_SHOWN]] if found else "none known"
    others = {sold for _, _, sold, _ in known.sales} - set(wanted)
    nearest = sorted((walk_order(f[0], w), code, f) for code in others if (f := _sellers(code, w, known)))
    more = len(nearest) - SELLER_ITEMS_SHOWN
    for _, code, found in nearest[:SELLER_ITEMS_SHOWN]:
        rows[code] = [_row(s, w) for s in found[:SELLERS_SHOWN]]
    if not rows:
        return "sellers=none known"
    return f"sellers={json.dumps(rows, sort_keys=True)}" + (f" (+{more} more items sold)" if more > 0 else "")


def _row(s: Seller, w: WorldModel) -> dict[str, Any]:
    steps = walk_estimate(s, w)
    row: dict[str, Any] = {"by": s.by, "at": [s.pos[0], s.pos[1]], "map_id": s.map_id}
    row["steps"] = steps if steps is not None else "other_map"
    if s.price is not None:
        row["price"] = s.price
    if not s.exact:
        row["search_radius"] = SELLER_SEARCH_RADIUS
    return row


def codes_with_sellers(w: WorldModel, kb: KnowledgeBase | None, codes: Collection[str]) -> set[str]:
    """Those of ``codes`` a seller is known for."""
    known = _known(kb)
    return {c for c in codes if _sellers(c, w, known)}
