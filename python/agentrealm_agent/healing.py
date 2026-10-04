"""Heal-state helpers: food, potions, safe-zone regen, buy signals (A10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Policy
from .item_table import HeldSupply
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import safe_tiles

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase
    from .memory import Memory

# GAME_NOTES.md Food / potions (M §16); subtype codes from observation.
FOOD_CODES = frozenset({"apple", "berry", "golden_cap"})
POTION_CODES = frozenset({"small_potion", "large_potion", "potion"})
DEFAULT_BUY_POTION = "small_potion"

# Ticks at 10 Hz before we conclude safe-zone regen is absent (~20 s).
REGEN_MEASURE_TICKS = 200

SURVIVAL_KEY = "survival"
REGEN_KEY = "safe_zone_regen"


def hurt(w: WorldModel) -> bool:
    if w.health is None or w.max_health is None:
        return False
    return w.health < w.max_health


def hostiles_in_range(w: WorldModel, policy: Policy) -> list[Entity]:
    here = w.pos
    if here is None:
        return []
    return [
        e
        for e in w.entities
        if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range
    ]


def is_food_code(code: str) -> bool:
    return code in FOOD_CODES


def is_potion_code(code: str) -> bool:
    return code in POTION_CODES


def food_in_sight(w: WorldModel) -> list[Entity]:
    here = w.pos
    if here is None:
        return []
    out = [e for e in w.entities if e.kind == "supply" and is_food_code(e.code)]
    out.sort(key=lambda e: (chebyshev(e.pos, here), e.id))
    return out


def held_supplies(w: WorldModel) -> list[HeldSupply]:
    return list(w.held)


def carried_potion(w: WorldModel) -> HeldSupply | None:
    for h in w.held:
        if is_potion_code(h.code):
            return h
    return None


def zone_safe(w: WorldModel, map_id: int, pos: Pos) -> bool | None:
    fact = w.zones.get(map_id, {}).get(pos)
    if fact is None:
        return None
    return fact.safe


def standing_in_safe_zone(w: WorldModel) -> bool:
    if w.map_id is None or w.pos is None:
        return False
    safe = zone_safe(w, w.map_id, w.pos)
    return safe is True


def regen_measurement(knowledge: KnowledgeBase | None) -> str | None:
    """``None`` unknown, ``yes`` regen observed, ``no`` measured absent."""
    if knowledge is None:
        return None
    surv = knowledge.extra.get(SURVIVAL_KEY)
    if not isinstance(surv, dict):
        return None
    val = surv.get(REGEN_KEY)
    if val in ("yes", "no"):
        return val
    return None


def set_regen_measurement(knowledge: KnowledgeBase | None, value: str) -> None:
    if knowledge is None or value not in ("yes", "no"):
        return
    with knowledge.lock:
        surv = knowledge.extra.setdefault(SURVIVAL_KEY, {})
        if not isinstance(surv, dict):
            knowledge.extra[SURVIVAL_KEY] = surv = {}
        surv[REGEN_KEY] = value


def note_regen_sample(m: Memory, w: WorldModel) -> None:
    """Start or continue a safe-zone regen observation while hurt."""
    if m.heal_regen_start_tick is None:
        m.heal_regen_start_tick = w.tick
        m.heal_regen_start_health = w.health
        return
    if w.health is None or m.heal_regen_start_health is None:
        return
    if w.health > m.heal_regen_start_health:
        m.heal_regen_measured = "yes"
    elif w.tick - m.heal_regen_start_tick >= REGEN_MEASURE_TICKS:
        m.heal_regen_measured = "no"


def flush_regen_measurement(m: Memory, knowledge: KnowledgeBase | None) -> None:
    if m.heal_regen_measured in ("yes", "no"):
        set_regen_measurement(knowledge, m.heal_regen_measured)
    m.heal_regen_start_tick = None
    m.heal_regen_start_health = None
    m.heal_regen_measured = None


def reset_regen_sample(m: Memory) -> None:
    m.heal_regen_start_tick = None
    m.heal_regen_start_health = None
    m.heal_regen_measured = None


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


def raise_buy_potion(m: Memory, *, why: str, code: str = DEFAULT_BUY_POTION) -> None:
    sig = (code, why)
    if sig in m.buy_signals_seen:
        return
    m.buy_signals_seen.add(sig)
    m.buy_signals.append({"op": "buy", "code": code, "why": why})
