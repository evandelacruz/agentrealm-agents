"""Heal-state helpers: food, potions, safe-zone regen, waits, buy signals (A10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .item_table import InventorySupply, merge_heal
from .knowledge_base import knowledge_items
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import safe_tiles

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase
    from .memory import Memory

# GAME_NOTES.md Items: golden cap (M §16), apples and berries on the ground
# in town (Obs). Whether apples and berries heal on pickup or carried and
# `Use`d is unmeasured (GAME_NOTES open measurements), so Heal tries both.
FOOD_CODES = frozenset({"apple", "berry", "golden_cap"})
# GAME_NOTES.md Items: small potion +10, large +30 (M §16).
POTION_CODES = frozenset({"small_potion", "large_potion"})
DEFAULT_BUY_POTION = "small_potion"


def potion_count(w: WorldModel) -> int:
    """Held and stowed potions (Boss preconditions, Shop reserve, A21, A38)."""
    codes = POTION_CODES
    n = sum(1 for h in w.held_supplies if h.code in codes)
    n += sum(1 for s in w.chest_supplies if s.code in codes)
    return n


def supply_matches(want: str, code: str) -> bool:
    """``code`` satisfies a want for ``want``: the same code, or any potion for a potion (A21)."""
    return want == code or (want in POTION_CODES and code in POTION_CODES)


# Ticks at 10 Hz in a safe zone with no health back before this run counts
# safe-zone regen as absent (~20 s).
REGEN_MEASURE_TICKS = 200
# A longer gap between Heal windows than this restarts the regen sample.
REGEN_SAMPLE_GAP_TICKS = 50
# Ticks Heal may send nothing with no health back before it yields to
# Explore, and how long it then stays out (~60 s and ~30 s).
HEAL_WAIT_TICKS = 600
HEAL_BACKOFF_TICKS = 300
# Times one Take or Use of the same supply is sent before Heal gives up on it.
HEAL_MAX_TRIES = 3

SURVIVAL_KEY = "survival"
REGEN_KEY = "safe_zone_regen"


def hurt(w: WorldModel) -> bool:
    if w.health is None or w.max_health is None:
        return False
    return w.health < w.max_health


def tries_left(m: Memory, kind: str, supply_id: int) -> bool:
    return m.heal_tries.get((kind, supply_id), 0) < HEAL_MAX_TRIES


def note_try(m: Memory, kind: str, supply_id: int) -> None:
    key = (kind, supply_id)
    m.heal_tries[key] = m.heal_tries.get(key, 0) + 1


def food_in_sight(w: WorldModel, m: Memory) -> list[Entity]:
    here = w.pos
    if here is None:
        return []
    out = [e for e in w.entities if e.kind == "supply" and e.code in FOOD_CODES and tries_left(m, "take", e.id)]
    out.sort(key=lambda e: (chebyshev(e.pos, here), e.id))
    return out


def carried_heal(w: WorldModel, m: Memory) -> InventorySupply | None:
    """Carried food first, then a potion (PLAYABLE_AGENT_PLAN.md Heal row)."""
    usable = [h for h in w.held_supplies if tries_left(m, "use", h.id)]
    for codes in (FOOD_CODES, POTION_CODES):
        for h in usable:
            if h.code in codes:
                return h
    return None


def standing_in_safe_zone(w: WorldModel) -> bool:
    if w.map_id is None or w.pos is None:
        return False
    fact = w.zones.get(w.map_id, {}).get(w.pos)
    return fact is not None and fact.safe is True


def regen_known(knowledge: KnowledgeBase | None, m: Memory) -> str | None:
    """``yes`` from the knowledge base, ``no`` measured this run, else ``None``."""
    if knowledge is not None:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        if isinstance(surv, dict) and surv.get(REGEN_KEY) == "yes":
            return "yes"
    return "no" if m.heal_regen_absent else None


def save_regen_yes(knowledge: KnowledgeBase | None) -> None:
    """Health came back in a safe zone: a fact for every run on this world."""
    if knowledge is None:
        return
    with knowledge.lock:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        if not isinstance(surv, dict):
            knowledge.extra[SURVIVAL_KEY] = surv = {}
        surv[REGEN_KEY] = "yes"


def note_regen_sample(m: Memory, w: WorldModel) -> str | None:
    """One window standing hurt in a safe zone: ``yes``, ``no``, or still measuring.

    The sample restarts when health falls or the last window seen is too far
    back (the character left the zone, or another state ran meanwhile).
    """
    if w.health is None:
        m.heal_regen_sample = None
        return None
    s = m.heal_regen_sample
    if s is None or w.health < s[1] or w.tick - s[2] > REGEN_SAMPLE_GAP_TICKS:
        m.heal_regen_sample = (w.tick, w.health, w.tick)
        return None
    start_tick, start_health, _ = s
    if w.health > start_health:
        m.heal_regen_sample = None
        return "yes"
    if w.tick - start_tick >= REGEN_MEASURE_TICKS:
        m.heal_regen_sample = None
        return "no"
    m.heal_regen_sample = (start_tick, start_health, w.tick)
    return None


def wait_exhausted(m: Memory, w: WorldModel) -> bool:
    """Count a window Heal sends nothing. True, and back off, once it has
    waited ``HEAL_WAIT_TICKS`` with no health back."""
    health = w.health if w.health is not None else 0
    if m.heal_wait is None or health > m.heal_wait[1]:
        m.heal_wait = (w.tick, health)
        return False
    start, low = m.heal_wait
    m.heal_wait = (start, min(low, health))
    if w.tick - start < HEAL_WAIT_TICKS:
        return False
    back_off(m, w)
    return True


def back_off(m: Memory, w: WorldModel) -> None:
    m.heal_backoff_until = w.tick + HEAL_BACKOFF_TICKS
    m.heal_wait = None
    m.heal_regen_sample = None
    if m.goal.startswith("heal_"):
        m.path, m.goal = [], ""


def nearest_known_safe(w: WorldModel) -> tuple[int, Pos] | None:
    """Nearest known safe cell on the current map, preferring cells near town."""
    here = w.pos
    map_id = w.map_id
    if here is None or map_id is None:
        return None
    candidates: list[tuple[int, int, Pos]] = []
    for pos in safe_tiles(w, map_id):
        near_town = any(
            am == map_id and chebyshev(pos, anchor) <= 8 for am, anchor in w.respawn_anchors
        )
        candidates.append((0 if near_town else 1, chebyshev(here, pos), pos))
    if not candidates:
        return None
    candidates.sort()
    return map_id, candidates[0][2]


def missing_health(w: WorldModel) -> int | None:
    if w.health is None or w.max_health is None:
        return None
    return max(0, w.max_health - w.health)


def heal_amount_known(knowledge: KnowledgeBase | None, code: str) -> int | None:
    row = knowledge_items(knowledge).get(code) or {}
    n = row.get("heal_amount")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


def food_worth_pickup(w: WorldModel, knowledge: KnowledgeBase | None, code: str) -> bool:
    """True when hurt enough to use this food's measured heal (A24).

    Unknown heal amounts are still tried (A10).
    """
    missing = missing_health(w)
    if missing is None or missing <= 0:
        return False
    amount = heal_amount_known(knowledge, code)
    if amount is None:
        return True
    return missing >= amount


def note_heal_pending(m: Memory, w: WorldModel, code: str, kind: str) -> None:
    if w.health is None or code not in FOOD_CODES | POTION_CODES:
        return
    m.heal_pending = (w.health, code, kind)


def absorb_heal_pending(
    m: Memory,
    w: WorldModel,
    knowledge: KnowledgeBase | None,
    events: list[dict],
) -> None:
    """Learn heal from the health change after a food ``Take`` or self-``Use``."""
    pending = m.heal_pending
    if pending is None or w.health is None or knowledge is None:
        m.heal_pending = None
        return
    start_health, code, kind = pending
    m.heal_pending = None
    if any(e.get("kind") == "Damaged" for e in events):
        return
    delta = w.health - start_health
    with knowledge.lock:
        if delta > 0:
            merge_heal(knowledge.items, code, delta, on_pickup=(kind == "take"))
        elif kind == "take":
            merge_heal(knowledge.items, code, 0, on_pickup=False)


def rearm_after_drink(w: WorldModel, m: Memory) -> list[dict] | None:
    """One ``Arm`` for the weapon that was swapped out for a drink (A24)."""
    code, m.heal_rearm = m.heal_rearm, None
    if code is None or w.armed_code == code:
        return None
    for h in w.held_supplies:
        if h.code == code:
            return [{"verb": "Arm", "supply_id": h.id}]
    return None


def raise_buy_potion(m: Memory, *, why: str, code: str = DEFAULT_BUY_POTION) -> None:
    """Queue a strategist ``buy`` op once per (code, why). Nothing reads
    ``buy_signals`` until Shop (A21)."""
    sig = (code, why)
    if sig in m.buy_signals_seen:
        return
    m.buy_signals_seen.add(sig)
    m.buy_signals.append({"op": "buy", "code": code, "why": why})
