"""Who sells an item, and where (A131).

A ``buy`` walks only toward a seller known to sell its item, never to a shop
that does not. A seller is known by one of:

- ``shop``: a shop cell seen selling the item (``travel.knowledge.iter_shop_sales``),
  with its gem price. The cell itself is where to go.
- ``clue``: a sign, scroll or NPC line that names the item and a gem price
  ("Raft, thirty gems"), at the cell the clue was found (``clues``). The
  seller is somewhere round it: Shop walks there and searches.
- ``plan``: the place the ``buy`` op names (``x``, ``y``, optional
  ``map_id``), the planner's reading of where a seller is, such as a town a
  clue names. Searched round the same way.

Of several, the cheapest walk is taken. With none known the buy is refused
when a reply puts it on the stack (``refuse_unsold``) and dropped by Shop,
never sent on a walk. This is the one test of whether a buy has a seller;
State ``no_seller_known`` (A110) and ``sellers`` read it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from .healing import supply_matches
from .knowledge_base import KnowledgeBase
from .travel.knowledge import iter_shop_sales
from .world import Pos, WorldModel, chebyshev

# How far round a seller's place (a clue's cell, a place the op names) Shop searches for it.
SELLER_SEARCH_RADIUS = 20
# The nearest sellers State shows per item.
SELLERS_SHOWN = 3

_GEMS = re.compile(r"\bgems?\b")


@dataclass(frozen=True)
class Seller:
    """Where an item is sold: the cell itself (``exact``), or a place to search round."""

    map_id: int
    pos: Pos
    by: str  # "shop", "clue" or "plan"
    exact: bool
    price: int | None = None


def _names(code: str, text: str) -> bool:
    """The text names the item: its code's words, in any case, plural allowed."""
    words = [re.escape(p) for p in code.lower().split("_") if p]
    return bool(words) and re.search(r"\b" + r"[\s_-]+".join(words) + r"s?\b", text.lower()) is not None


def _clue_sellers(code: str, kb: KnowledgeBase) -> list[Seller]:
    with kb.lock:
        rows = list(kb.clues)
    out = []
    for row in rows:
        text = row.get("text")
        if not isinstance(text, str) or not _GEMS.search(text.lower()) or not _names(code, text):
            continue
        try:
            out.append(Seller(int(row["map_id"]), (int(row["x"]), int(row["y"])), "clue", False))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _op_seller(op: dict[str, Any] | None, w: WorldModel) -> Seller | None:
    if not op or "x" not in op or "y" not in op:
        return None
    map_id = op.get("map_id", w.map_id)
    if map_id is None:
        return None
    return Seller(int(map_id), (int(op["x"]), int(op["y"])), "plan", False)


def sellers_of(code: str, w: WorldModel, kb: KnowledgeBase | None, op: dict[str, Any] | None = None) -> list[Seller]:
    """Every seller of ``code`` known, cheapest walk first."""
    found: list[Seller] = []
    if (named := _op_seller(op, w)) is not None:
        found.append(named)
    if kb is not None:
        found += [
            Seller(mid, pos, "shop", True, price)
            for mid, pos, sold, price in iter_shop_sales(kb)
            if supply_matches(code, sold)
        ]
        found += _clue_sellers(code, kb)
    unique = list(dict.fromkeys(found))
    return sorted(unique, key=lambda s: walk_order(s, w))


def walk_estimate(s: Seller, w: WorldModel) -> int | None:
    """Roughly how many steps the walk to ``s`` is; None on another map."""
    if w.pos is None or s.map_id != w.map_id:
        return None
    return chebyshev(w.pos, s.pos)


def walk_order(s: Seller, w: WorldModel) -> tuple:
    """Cheapest walk first: this map before another, then fewer steps."""
    steps = walk_estimate(s, w)
    return (steps is None, steps or 0, s.map_id, s.pos, s.by)


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
                "no clue naming it with a gem price); find one first, or give x, y for the place a clue says sells it"
            )
            continue
        kept.append(g)
    return kept, refused


def sellers_line(w: WorldModel, kb: KnowledgeBase | None, wanted: Collection[str] = ()) -> str:
    """Where each item is sold, nearest first with roughly how far (A131):
    every item a seller is known for, and each item the plan wants to buy
    (``none known`` when it has none)."""
    codes = set(wanted)
    if kb is not None:
        codes |= {sold for _, _, sold, _ in iter_shop_sales(kb)}
    rows: dict[str, Any] = {}
    for code in sorted(codes):
        found = sellers_of(code, w, kb)
        if not found:
            rows[code] = "none known"
            continue
        rows[code] = [_row(s, w) for s in found[:SELLERS_SHOWN]]
    return f"sellers={json.dumps(rows, sort_keys=True)}" if rows else "sellers=none known"


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
    return {c for c in codes if sellers_of(c, w, kb)}
