"""Our death chests as a source of items (A167).

A death drops everything carried but the starting kit into a chest where
the character fell (Manual §11 Death, lives, and ending), and the chest
stays until it is emptied. So an item a goal wants may already be ours,
lying in a chest a walk away: ``WorldModel.death_chests`` knows what each
holds, expected from what was carried at the death until a look in reach
shows what it holds (``DeathChest.seen``).

One rule, which Gary's buy refusal and Lorraine's nodes both read
(:func:`source_for`): a death chest on this map holding the item is the
source ahead of buying it while the walk there (``healing.walk_seconds``)
takes no longer than earning the item's price in gems at the gather's
measured rate (``gem_yield.minutes_at``, the minutes State shows). A buy
spends its gems whether they are held or not, so they are earned again
either way. With no rate measured there is no time to weigh against: the
chest, which costs no gems, wins. The rule lets go of a chest it cannot be
sure of: one a walk to has been given up or stalled on (``unreachable``),
and one dropped more than ``SOURCE_TICKS`` ago, which anyone may have
emptied since. A chest on another map has no walk to price. Each stays in
the planner's State for it to weigh; the rule never chooses it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .discovery import seen_prices
from .gem_yield import minutes_at
from .healing import supply_matches, walk_seconds
from .travel.knowledge import iter_shop_sales
from .world import Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

# A chest dropped this long ago (ticks: 10 minutes at 10 Hz) is no longer
# chosen over a buy by itself: anyone may have emptied it.
SOURCE_TICKS = 6000


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
    died: int = 0


def chest_items(w: WorldModel) -> list[ChestItem]:
    """Every item our remembered death chests hold, nearest chest first, this map's before another's."""
    out = []
    for chest_id, c in w.death_chests.items():
        here = c.map_id == w.map_id and w.pos is not None
        steps = chebyshev(w.pos, c.pos) if here and w.pos is not None else None
        out += [ChestItem(chest_id, c.map_id, c.pos, code, steps, c.seen, c.died) for code in c.contents or ()]
    return sorted(out, key=lambda i: (i.steps is None, i.steps or 0, i.map_id, i.chest_id, i.code))


def known_prices(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, int]:
    """Each item's gem price as seen for sale: the cheapest a shop cell was
    seen selling it at, an ``items`` row's, or one priced in sight."""
    prices = dict(seen_prices(w, kb))
    for _, _, code, price in iter_shop_sales(kb):
        if isinstance(price, int) and price > 0 and price < prices.get(code, price + 1):
            prices[code] = price
    return prices


def earn_seconds(gems: int, gems_per_min: float | None) -> float | None:
    """How long ``gems`` take to earn, in seconds, by the minutes State shows
    (``gem_yield.minutes_at``): None with no rate measured, forever at a rate of 0."""
    minutes = minutes_at(gems, gems_per_min)
    if minutes is None:
        return None
    return minutes * 60 if isinstance(minutes, float) else float("inf")


def source_for(
    code: str,
    w: WorldModel,
    price: int | None,
    gems_per_min: float | None = None,
    matches: Callable[[str, str], bool] = supply_matches,
    unreachable: Collection[tuple[int, Pos]] = (),
) -> ChestItem | None:
    """The nearest death chest on this map holding an item ``matches`` takes
    for ``code``, when it is the source ahead of buying: the walk there takes
    no longer than earning ``price`` gems, or there is nothing to weigh it
    against (no price, no rate measured). A chest a walk was given up or
    stalled on (a cell of ``unreachable`` within a step of it), or one
    dropped more than ``SOURCE_TICKS`` ago, is never chosen."""
    return next(
        (i for i in chest_items(w) if matches(code, i.code) and is_source(i, w, price, gems_per_min, unreachable)),
        None,
    )


def is_source(
    item: ChestItem,
    w: WorldModel,
    price: int | None,
    gems_per_min: float | None = None,
    unreachable: Collection[tuple[int, Pos]] = (),
) -> bool:
    """``item`` is a source ahead of buying at ``price`` (:func:`source_for`'s
    rule, for one copy in one chest): on this map, fresh, not at a cell of
    ``unreachable``, and walked to no slower than ``price`` gems are earned."""
    if item.steps is None or w.tick - item.died > SOURCE_TICKS:
        return False
    if any(mid == item.map_id and chebyshev(pos, item.pos) <= 1 for mid, pos in unreachable):
        return False
    earn = None if price is None else earn_seconds(price, gems_per_min)
    return earn is None or walk_seconds(w, item.pos) <= earn


def in_reach(item: ChestItem, w: WorldModel) -> bool:
    """The chest is on or next to the character's block, where ``WithdrawFromChest`` reaches it."""
    return item.steps is not None and item.steps <= 1


def refuse_chest_held(
    goals: list[dict[str, Any]],
    w: WorldModel,
    prices: Mapping[str, int],
    gems_per_min: float | None = None,
    unreachable: Collection[tuple[int, Pos]] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ``buy`` ops whose item a death chest of ours on this map
    holds, out of reach, with the walk there no longer than earning its
    price (:func:`source_for`), are left out, each with why: the chest is the
    source ahead of buying (A167). One in reach is the withdraw's to take;
    one ``unreachable`` or stale lets the buy through."""
    kept, refused = [], []
    for g in goals:
        code = g.get("code") if g.get("op") == "buy" else None
        item = source_for(code, w, prices.get(code), gems_per_min, unreachable=unreachable) if isinstance(code, str) else None
        if item is None or in_reach(item, w):
            kept.append(g)
            continue
        price = prices.get(code)
        held = "holds" if item.seen else "is expected to hold (what was carried at the death)"
        earn_s = None if price is None else earn_seconds(price, gems_per_min)
        if price is None:
            earn = "it has no known price"
        elif earn_s is None:
            earn = f"no gem rate is measured to weigh its {price} gems against"
        else:
            earn = f"earning its {price} gems takes about {earn_s:.0f} s"
        refused.append(
            f"{json.dumps(g, sort_keys=True)} left out: death chest {item.chest_id} at {item.pos[0]}, {item.pos[1]} "
            f"{held} {item.code}, {item.steps} steps (about {walk_seconds(w, item.pos)} s) away, and {earn}: travel to point at its cell "
            "to take it back first; buy it once the chest is seen without it"
        )
    return kept, refused
