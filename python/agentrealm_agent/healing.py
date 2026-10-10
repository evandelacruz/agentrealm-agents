"""Heal-state helpers: food, potions, safe-zone regen (A10)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .item_table import InventorySupply, merge_heal
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import known_safe, safe_tiles

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase
    from .memory import Memory

# GAME_NOTES.md Items: golden cap (M §16), apples and berries on the ground
# in town (Obs). Whether apples and berries heal on pickup or carried and
# `Use`d is unmeasured (GAME_NOTES open measurements), so Heal tries both and
# files what each did in the item table (A24).
FOOD_CODES = frozenset({"apple", "berry", "golden_cap"})
# GAME_NOTES.md Items: small potion +10, large +30 (M §16).
POTION_CODES = frozenset({"small_potion", "large_potion"})


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


def standing_in_safe_zone(w: WorldModel, pos: Pos | None = None) -> bool:
    """Whether ``pos`` (default: where the character stands) is a known safe-zone cell."""
    at = w.pos if pos is None else pos
    if w.map_id is None or at is None:
        return False
    return known_safe(w, w.map_id, at)


def note_heal_window(m: Memory, w: WorldModel) -> None:
    """Once per decision, whatever state runs: a full heal re-arms the
    ``heal_supplies`` ask, so the next hurt spell asks the planner again."""
    if w.health is not None and not hurt(w):
        m.heal_supplies_asked = False


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
    """One window hurt in a safe zone: ``yes``, ``no``, or still measuring.

    The sample restarts when health falls or the last window seen is too far
    back (the character left the zone, or another state ran meanwhile).

    Only a hurt character can show regen: health back counts as "yes" even
    when it reaches full, but a window at full health (or with ``max_health``
    unknown) starts no sample and gives no verdict. A "no" is only ever a hurt
    window with no health back.
    """
    s = m.heal_regen_sample
    if w.health is None:
        m.heal_regen_sample = None
        return None
    fresh = s is not None and w.health >= s[1] and w.tick - s[2] <= REGEN_SAMPLE_GAP_TICKS
    if fresh and w.health > s[1]:
        m.heal_regen_sample = None
        return "yes"
    if not hurt(w):
        m.heal_regen_sample = None
        return None
    if not fresh:
        m.heal_regen_sample = (w.tick, w.health, w.tick)
        return None
    start_tick, start_health, _ = s
    if w.tick - start_tick >= REGEN_MEASURE_TICKS:
        m.heal_regen_sample = None
        return "no"
    m.heal_regen_sample = (start_tick, start_health, w.tick)
    return None


def known_safe_cells(w: WorldModel) -> list[Pos]:
    """The known safe cells on the current map, those near town first, then
    nearest first (ties to the smaller cell). Heal path-checks them in this
    order (``pathing.reachable_safe_goal``)."""
    here, map_id = w.pos, w.map_id
    if here is None or map_id is None:
        return []
    anchors = [anchor for am, anchor in w.respawn_anchors if am == map_id]

    def order(pos: Pos) -> tuple[int, int, Pos]:
        near_town = any(chebyshev(pos, anchor) <= 8 for anchor in anchors)
        return (0 if near_town else 1, chebyshev(here, pos), pos)

    return sorted(safe_tiles(w, map_id), key=order)


def self_use_code(w: WorldModel, before: dict | None) -> str | None:
    """The code a self-``Use`` drinks or eats, given the intent queued just before it.

    Heal sends ``[Arm item, Use self]`` in one queue, and both results are
    applied before the observation updates ``armed_code``. So an ``Arm``
    just before the ``Use`` names the item; with no ``Arm`` there, the item
    was already armed.
    """
    if before and before.get("verb") == "Arm":
        for h in w.held_supplies:
            if h.id == before.get("supply_id"):
                return h.code
        return None
    return w.armed_code


def note_heal_pending(m: Memory, w: WorldModel, code: str, kind: str) -> None:
    """Remember health before a food ``Take`` or self-``Use`` (``kind`` is
    "take" or "use"), so the next observation can show what it healed (A24).

    Only while hurt: at full health nothing can heal, so nothing is learned.
    """
    if not hurt(w) or code not in FOOD_CODES | POTION_CODES:
        return
    assert w.health is not None
    m.heal_pending = (w.health, code, kind)


def absorb_heal_pending(
    m: Memory,
    w: WorldModel,
    knowledge: KnowledgeBase | None,
    events: list[dict],
) -> None:
    """File the health change after a food ``Take`` or self-``Use`` (A24).

    A response that also took damage is skipped: the change is not the
    item's alone. A ``Take`` that healed files ``heal_on_pickup`` True, one
    that healed nothing files False. A ``Use`` says nothing about pickup.
    """
    pending, m.heal_pending = m.heal_pending, None
    if pending is None or w.health is None or knowledge is None:
        return
    start_health, code, kind = pending
    if any(e.get("kind") == "Damaged" for e in events):
        return
    healed = w.health - start_health
    on_pickup = (healed > 0) if kind == "take" else None
    with knowledge.lock:
        merge_heal(knowledge.items, code, healed, on_pickup=on_pickup)


def rearm_after_drink(w: WorldModel, m: Memory) -> list[dict] | None:
    """One ``Arm`` for the weapon a drink swapped out, sent once (A24).

    Clears ``heal_rearm`` either way, so a rejected ``Arm`` or a weapon no
    longer in hand cannot keep Heal (or Equip, which waits on it) stuck.
    """
    code, m.heal_rearm = m.heal_rearm, None
    if code is None or w.armed_code == code:
        return None
    for h in w.held_supplies:
        if h.code == code:
            return [{"verb": "Arm", "supply_id": h.id}]
    return None
