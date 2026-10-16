"""Who sells an item, and where (A131).

A ``buy`` walks only toward a seller known to sell its item, never to a shop
that does not. ``sellers_of`` is the one seller test. A seller is known by:

- ``shop``: a shop cell seen selling the item (``travel.knowledge.iter_shop_sales``),
  with its gem price. The cell itself is where to go. One seen since without
  it is sold out until its stock is back (``back``, A159: stock returns two
  minutes after it is bought, ``note_sold_out``): still its seller, but not
  walked to and no buy's seller until then; the sale lapses only after
  ``SALE_LAPSE_MISSES`` misses in a row, each after its stock was due back.
- ``clue``: a sign, scroll or NPC line with a phrase that names the item and
  a gem price ("Raft, thirty gems"), at the cell the clue was found
  (``clues``). The seller is somewhere round it: Shop walks there and searches.
- ``plan``: the place the ``buy`` op names (``x``, ``y``, ``map_id``), the
  planner's reading of where a seller is, such as a town a clue names.
  Searched round the same way.
A clue's or the op's place searched round without the item is no seller of
it (``note_not_sold``) until a clue offering the item is found after that
search, which lapses the record (``lapse_not_sold``), or a shop is seen
selling it.

- ``unchecked``: a shop cell saved before A131, with no record of what it
  sells, for an item a price was seen for. It is taken only after every
  other seller, and checked off once seen (``note_sold_out``).

A price seen with no place is no seller. Of several, the cheapest walk is
taken. With none known, or only shops sold out, the buy is refused when a
reply puts it on the stack (``refuse_unsold``) and dropped by Shop, never
sent on a walk. State
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
from .travel.knowledge import iter_not_sold, iter_shop_sales, iter_sold_out, iter_unchecked_shop_cells
from .travel.knowledge import lapse_not_sold as _lapse
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
    back: int | None = None  # a sold-out shop cell: the tick its stock is back (A159)

    @property
    def in_stock(self) -> bool:
        """Not known sold out: a buy may walk to it (A159)."""
        return self.back is None


@dataclass
class _Known:
    """What the knowledge base says of sellers, read once for many items."""

    sales: list[tuple[int, Pos, str, int | None]]
    clues: list[dict[str, Any]]
    unchecked: list[tuple[int, Pos]]
    priced: dict[str, int]
    not_sold: dict[tuple[int, Pos, str], int]
    sold_out: dict[tuple[int, Pos, str], int]


def _known(kb: KnowledgeBase | None) -> _Known:
    if kb is None:
        return _Known([], [], [], {}, {}, {})
    with kb.lock:
        clues = list(kb.clues)
    priced = {
        code: row["gem_price"]
        for code, row in knowledge_items(kb).items()
        if isinstance(row, dict) and isinstance(row.get("gem_price"), int) and row["gem_price"] > 0
    }
    return _Known(iter_shop_sales(kb), clues, iter_unchecked_shop_cells(kb), priced, iter_not_sold(kb), iter_sold_out(kb))


def _names(code: str, text: str) -> bool:
    """The text names the item: its code's words, in any case, plural allowed."""
    words = [re.escape(p) for p in code.lower().split("_") if p]
    return bool(words) and re.search(r"\b" + r"[\s_-]+".join(words) + r"(?:e?s)?\b", text) is not None


def _offers(code: str, text: str) -> bool:
    """One phrase of the text names the item and a gem price."""
    return any(_GEMS.search(p) and _names(code, p) for p in _PHRASE.split(text.lower()))


def _offer_ticks(code: str, known: _Known) -> list[tuple[Seller, int | None]]:
    """Each clue offering ``code``: its place and the tick it was found."""
    out = []
    for row in known.clues:
        text = row.get("text")
        if not isinstance(text, str) or not _offers(code, text):
            continue
        try:
            s = Seller(int(row["map_id"]), (int(row["x"]), int(row["y"])), "clue", False)
        except (KeyError, TypeError, ValueError):
            continue
        tick = row.get("tick")
        out.append((s, tick if isinstance(tick, int) else None))
    return out


def _barred(code: str, s: Seller, known: _Known) -> bool:
    """``s``'s place was searched without ``code`` for sale, and no clue
    offering it was found since: it is no seller of it (A131)."""
    searched = known.not_sold.get((s.map_id, s.pos, code))
    if searched is None:
        return False
    return not any(t is not None and t > searched for _, t in _offer_ticks(code, known))


def _clue_sellers(code: str, known: _Known) -> list[Seller]:
    """Each clue's place that offers ``code``, unless searched without it since."""
    return [s for s, _ in _offer_ticks(code, known) if not _barred(code, s, known)]


def _op_seller(code: str, op: dict[str, Any] | None, known: _Known) -> Seller | None:
    """The place the op names (``_validate_buy`` takes it only with its map),
    unless searched without the item since the newest clue offering it."""
    if not op or not all(k in op for k in ("x", "y", "map_id")):
        return None
    s = Seller(int(op["map_id"]), (int(op["x"]), int(op["y"])), "plan", False)
    return None if _barred(code, s, known) else s


def lapse_not_sold(kb: KnowledgeBase | None) -> None:
    """Drop every searched-without record a clue found since has revived: a
    clue offering the item lapses its records, wherever they are (A131)."""
    known = _known(kb)
    revived = {code for (mid, pos, code) in known.not_sold if not _barred(code, Seller(mid, pos, "plan", False), known)}
    _lapse(kb, revived)


def _sellers(code: str, w: WorldModel, known: _Known, op: dict[str, Any] | None = None) -> list[Seller]:
    found: list[Seller] = []
    if (named := _op_seller(code, op, known)) is not None:
        found.append(named)
    found += [
        Seller(mid, pos, "shop", True, price, _back(known, mid, pos, sold, w))
        for mid, pos, sold, price in known.sales
        if supply_matches(code, sold)
    ]
    found += _clue_sellers(code, known)
    price = next((p for c, p in known.priced.items() if supply_matches(code, c)), None)
    if price is not None:
        found += [Seller(mid, pos, "unchecked", True) for mid, pos in known.unchecked]
    return sorted(dict.fromkeys(found), key=lambda s: walk_order(s, w))


def _back(known: _Known, map_id: int, pos: Pos, code: str, w: WorldModel) -> int | None:
    """The tick a shop cell's sold-out stock of ``code`` is back, while it is not yet (A159)."""
    back = known.sold_out.get((map_id, pos, code))
    return back if back is not None and w.tick < back else None


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
    shop not known to sell the item only after every one that is, and a
    sold-out one last (A159)."""
    steps = walk_estimate(s, w)
    return (not s.in_stock, s.by == "unchecked", steps is None, steps or 0, s.map_id, s.pos, s.by)


def for_sale_in_sight(code: str, w: WorldModel) -> bool:
    return any(e.kind == "supply" and isinstance(e.gem_price, int) and e.gem_price > 0 and supply_matches(code, e.code) for e in w.entities)


def seller_known(code: str, w: WorldModel, kb: KnowledgeBase | None, op: dict[str, Any] | None = None) -> bool:
    """A buy of ``code`` has somewhere to go: a seller known and not sold out, or the item for sale in sight."""
    return for_sale_in_sight(code, w) or any(s.in_stock for s in sellers_of(code, w, kb, op))


def sold_out_until(code: str, w: WorldModel, kb: KnowledgeBase | None, op: dict[str, Any] | None = None) -> int | None:
    """When every known seller of ``code`` is a sold-out shop: the first tick one is back (A159)."""
    found = sellers_of(code, w, kb, op)
    if not found or any(s.in_stock for s in found):
        return None
    return min(s.back for s in found if s.back is not None)


def refuse_unsold(
    goals: list[dict[str, Any]], w: WorldModel, kb: KnowledgeBase | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ``buy`` ops with no seller known are left out, each with why (A110, A131)."""
    kept, refused = [], []
    for g in goals:
        if g.get("op") == "buy" and not seller_known(g["code"], w, kb, g):
            back = sold_out_until(g["code"], w, kb, g)
            if back is not None:
                refused.append(
                    f"{json.dumps(g, sort_keys=True)} left out: every known seller of {g['code']} is sold out "
                    f"until tick {back} (shop stock returns two minutes after it is bought); buy it then"
                )
                continue
            refused.append(
                f"{json.dumps(g, sort_keys=True)} left out: no seller of {g['code']} is known (no shop seen selling it, "
                "no clue naming it with a gem price); find one first, or give x, y and map_id for the place a clue says sells it"
            )
            continue
        kept.append(g)
    return kept, refused


def _buys(ops: Collection[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The stack's buy ops by the code each buys."""
    out: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        if op.get("op") == "buy" and isinstance(op.get("code"), str):
            out.setdefault(op["code"], []).append(op)
    return out


def _sellers_for_ops(code: str, w: WorldModel, known: _Known, ops: Collection[dict[str, Any]]) -> list[Seller]:
    """``code``'s sellers, each buy op's own place among them (the op's own resolution)."""
    found = [s for op in ops for s in _sellers(code, w, known, op)] or _sellers(code, w, known)
    return sorted(dict.fromkeys(found), key=lambda s: walk_order(s, w))


def sellers_line(w: WorldModel, kb: KnowledgeBase | None, stack: Collection[dict[str, Any]] = ()) -> str:
    """Where each item is sold, nearest first with roughly how far (A131):
    each item a buy op on ``stack`` wants (its own place included; ``none
    known`` when it has none), then up to ``SELLER_ITEMS_SHOWN`` more a shop
    is known to sell, nearest first."""
    known = _known(kb)
    wanted = _buys(stack)
    rows: dict[str, Any] = {}
    for code in sorted(wanted):
        found = _sellers_for_ops(code, w, known, wanted[code])
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
    if s.back is not None:
        row["sold_out_until_tick"] = s.back
    if not s.exact:
        row["search_radius"] = SELLER_SEARCH_RADIUS
    return row


def codes_with_sellers(
    w: WorldModel, kb: KnowledgeBase | None, codes: Collection[str], stack: Collection[dict[str, Any]] = ()
) -> set[str]:
    """Those of ``codes`` a seller is known for, a stack buy op's own place included."""
    known, wanted = _known(kb), _buys(stack)
    return {c for c in codes if _sellers_for_ops(c, w, known, wanted.get(c, ()))}
