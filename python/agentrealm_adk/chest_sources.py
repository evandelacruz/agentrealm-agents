"""Our death chests as a source of items (A167).

A death drops everything carried but the starting kit into a chest where
the character fell (Manual §11 Death, lives, and ending), and the chest
stays until it is emptied. So an item a goal wants may already be ours,
lying in a chest a walk away: ``WorldModel.death_chests`` knows what each
holds, expected from what was carried at the death until a look in reach
shows what it holds (``DeathChest.seen``).

One rule, which Gary's buy refusal and Lorraine's nodes both read
(:func:`source_for`): a death chest on this map holding the item is the
source ahead of buying it when the walk there takes no longer than earning
the item's price in gems. A buy spends its gems whether they are held or
not, so the gems are earned again either way. The gems a minute are the
gather's measured rate, else ``FIELD_GEMS_PER_MIN`` (field work, Manual
§16 How to make money). A chest on another map has no walk to price: it is
shown, never chosen by itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from typing import TYPE_CHECKING

from .discovery import seen_prices
from .healing import supply_matches
from .travel.knowledge import iter_shop_sales
from .world import Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

# Gems a minute assumed for earning a price until a gather measures a rate
# (Manual §16: field work makes about 6 gems a minute).
FIELD_GEMS_PER_MIN = 6.0


@dataclass(frozen=True)
class ChestItem:
    """One item one of our death chests holds: where the chest lies, the
    walk there (``steps``, None on another map) and whether its contents
    were ``seen`` or are expected from the death."""

    chest_id: int
    map_id: int
    pos: Pos
    code: str
    steps: int | None
    seen: bool


def chest_items(w: WorldModel) -> list[ChestItem]:
    """Every item our remembered death chests hold, nearest chest first, this map's before another's."""
    out = []
    for chest_id, c in w.death_chests.items():
        here = c.map_id == w.map_id and w.pos is not None
        steps = chebyshev(w.pos, c.pos) if here and w.pos is not None else None
        out += [ChestItem(chest_id, c.map_id, c.pos, code, steps, c.seen) for code in c.contents or ()]
    return sorted(out, key=lambda i: (i.steps is None, i.steps or 0, i.map_id, i.chest_id, i.code))


def known_prices(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, int]:
    """Each item's gem price as seen for sale: the cheapest a shop cell was
    seen selling it at, an ``items`` row's, or one priced in sight."""
    prices = dict(seen_prices(w, kb))
    for _, _, code, price in iter_shop_sales(kb):
        if isinstance(price, int) and price > 0 and price < prices.get(code, price + 1):
            prices[code] = price
    return prices


def walk_seconds(item: ChestItem, w: WorldModel) -> float | None:
    """The straight walk to ``item``'s chest at the character's speed; None on another map."""
    return None if item.steps is None else item.steps * 1000 / max(1, w.movement_speed)


def earn_seconds(gems: int, gems_per_min: float | None = None) -> float:
    """How long ``gems`` take to earn at ``gems_per_min`` (``FIELD_GEMS_PER_MIN``
    when none is measured); forever at a measured rate of 0."""
    rate = FIELD_GEMS_PER_MIN if gems_per_min is None else gems_per_min
    return gems * 60 / rate if rate > 0 else float("inf")


def source_for(
    code: str,
    w: WorldModel,
    price: int | None,
    gems_per_min: float | None = None,
    matches: Callable[[str, str], bool] = supply_matches,
) -> ChestItem | None:
    """The nearest death chest on this map holding an item ``matches`` takes
    for ``code``, when it is the source ahead of buying: the walk there takes
    no longer than earning ``price`` gems, or the item has no price (no
    seller: the chest is the only source known)."""
    for item in chest_items(w):
        if item.steps is None or not matches(code, item.code):
            continue
        walk = walk_seconds(item, w) or 0.0
        if price is None or walk <= earn_seconds(price, gems_per_min):
            return item
    return None


def in_reach(item: ChestItem, w: WorldModel) -> bool:
    """The chest is on or next to the character's block, where ``WithdrawFromChest`` reaches it."""
    return item.steps is not None and item.steps <= 1


def refuse_chest_held(
    goals: list[dict[str, Any]],
    w: WorldModel,
    prices: Mapping[str, int],
    gems_per_min: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ``buy`` ops whose item a death chest of ours on this map
    holds, out of reach, with the walk there no longer than earning its
    price (:func:`source_for`), are left out, each with why: the chest is the
    source ahead of buying (A167). One in reach is the withdraw's to take."""
    kept, refused = [], []
    for g in goals:
        code = g.get("code") if g.get("op") == "buy" else None
        item = source_for(code, w, prices.get(code), gems_per_min) if isinstance(code, str) else None
        if item is None or in_reach(item, w):
            kept.append(g)
            continue
        price = prices.get(code)
        held = "holds" if item.seen else "is expected to hold (what was carried at the death)"
        walk = round(walk_seconds(item, w) or 0)
        earn = "it has no known price" if price is None else f"earning its {price} gems takes about {round(earn_seconds(price, gems_per_min))} s"
        refused.append(
            f"{json.dumps(g, sort_keys=True)} left out: death chest {item.chest_id} at {item.pos[0]}, {item.pos[1]} "
            f"{held} {item.code}, {item.steps} steps (about {walk} s) away, and {earn}: travel to point at its cell "
            "to take it back first; buy it once the chest is seen without it"
        )
    return kept, refused
