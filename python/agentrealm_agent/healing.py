"""What heals and when (A10): food, potions, safe-zone regen, and the line
below which health is low.

Food and potions are the Supplies reference's (A54, ``supplies.is_food``,
``supplies.is_potion``). Food is tried both ways, a ``Take`` and a carried
``Use``, and what each did is filed in the item table (A24). What is sent
after a refused or no-effect ``Take`` or ``Use`` is the executor's
(``executor.supply_refusals``); Heal only asks whether a supply may be
tried (``can_try``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .directives import PARAM_DEFAULTS
from .engagement import drink_turns_fight
from .executor.supply_refusals import can_try, in_hand
from .item_table import InventorySupply, merge_heal
from .supplies import heals, is_food, is_potion, row
from .world import Pos, WorldModel, chebyshev
from .zone_discovery import known_safe, safe_tiles

if TYPE_CHECKING:
    from .config import Policy
    from .knowledge_base import KnowledgeBase
    from .memory import Memory


def potion_count(w: WorldModel) -> int:
    """Held, stowed and armed potions (Boss preconditions, Shop reserve, A21,
    A38). The armed one counts when ``held`` leaves it out, so a drink that
    moved a potion into the slot and drank nothing is not a potion gone (A76)."""
    n = sum(1 for h in w.held_supplies if is_potion(h.code))
    n += sum(1 for s in w.chest_supplies if is_potion(s.code))
    if w.armed_id is not None and is_potion(w.armed_code) and all(h.id != w.armed_id for h in w.held_supplies):
        n += 1
    return n


def supply_matches(want: str, code: str) -> bool:
    """``code`` satisfies a want for ``want``: the same code, or any potion for a potion (A21)."""
    return want == code or (is_potion(want) and is_potion(code))


# Ticks at 10 Hz in a safe zone with no health back before this run counts
# safe-zone regen as absent (~20 s).
REGEN_MEASURE_TICKS = 200
# A longer gap between Heal windows than this restarts the regen sample.
REGEN_SAMPLE_GAP_TICKS = 50
SURVIVAL_KEY = "survival"
REGEN_KEY = "safe_zone_regen"
# Health a tick the "yes" window showed: rough, a floor (A124).
REGEN_RATE_KEY = "safe_zone_regen_per_tick"


def hurt(w: WorldModel) -> bool:
    if w.health is None or w.max_health is None:
        return False
    return w.health < w.max_health


Params = dict[str, float | int]


def low_health_share(params: Params) -> float:
    """The share of max health at or below which health is low: the
    planner's ``low_health`` param (``plan.PARAM_MEANINGS``)."""
    return float(params.get("low_health", PARAM_DEFAULTS["low_health"]))


def health_low(w: WorldModel, params: Params) -> bool:
    """Health is at or below ``low_health`` of max. Below it a carried potion
    is spent (``spend_potion``) and Heal's rest on safe ground goes ahead of
    the plan's op; above it the planner's op and the potion reserve go first."""
    if w.health is None or w.max_health is None:
        return False
    return w.health <= w.max_health * low_health_share(params)


# When a carried potion is spent (A96), in the planner's words: the same rule
# ``spend_potion`` applies, so a reserve the planner buys is one the
# character keeps.
POTION_RULE = (
    "A carried potion is drunk only when health is low (at or below the low_health share of max, "
    "potion_drunk_at under State), or mid-fight when the win estimate says the drink turns the fight "
    "(to one it would take on, or would win). Otherwise a hurt character eats food or rests on safe ground "
    "and keeps its potions, so potions held are a reserve that lasts until then."
)


def low_health_line(w: WorldModel, params: Params) -> int | None:
    """The highest health ``health_low`` counts as low, or None unknown."""
    if w.max_health is None:
        return None
    return int(w.max_health * low_health_share(params))


def spend_potion(w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int], code: str) -> bool:
    """Whether a carried potion of ``code`` is worth drinking now (A96):
    health is low (``health_low``), or the drink turns the engagement under
    way (``engagement.drink_turns_fight``). Otherwise rest or food heals,
    and the potion stays a reserve. Every drink of a potion asks this."""
    if health_low(w, params):
        return True
    r = row(code)
    heal = r.heal if r is not None and r.heal else (w.max_health or 0)  # unknown: counted as a full heal
    return drink_turns_fight(w, m, policy, params, heal)


def carried_heal(
    w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int]
) -> InventorySupply | None:
    """Carried food first, then a potion (PLAYABLE_AGENT_PLAN.md Heal row),
    a potion only when ``spend_potion`` says it is worth drinking now.
    The armed supply counts too: the snapshot may leave it out of ``held``
    (GAME_NOTES open questions), and a potion a drink left armed is still one
    to drink (A76)."""
    usable = [h for h in in_hand(w) if can_try(m, w, "use", h.id)]
    for h in usable:
        if is_food(h.code):
            return h
    # Each code heals its own amount, so each is asked: a bigger heal may turn a fight a smaller one does not.
    for h in usable:
        if is_potion(h.code) and spend_potion(w, m, policy, params, h.code):
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


def save_regen_yes(knowledge: KnowledgeBase | None, per_tick: float | None = None) -> None:
    """Health came back in a safe zone: a fact for every run on this world,
    with the health a tick the window showed when it is known (A124)."""
    if knowledge is None:
        return
    with knowledge.lock:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        if not isinstance(surv, dict):
            knowledge.extra[SURVIVAL_KEY] = surv = {}
        surv[REGEN_KEY] = "yes"
        if per_tick is not None and per_tick > 0:
            surv[REGEN_RATE_KEY] = per_tick


def regen_per_tick(knowledge: KnowledgeBase | None) -> float | None:
    """Safe-zone regen measured on this world, health a tick, or None: the
    published docs give health back only from a heal or a respawn, so with
    none measured a rest heals nothing (A124)."""
    if knowledge is None:
        return None
    with knowledge.lock:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        rate = surv.get(REGEN_RATE_KEY) if isinstance(surv, dict) and surv.get(REGEN_KEY) == "yes" else None
    return float(rate) if isinstance(rate, (int, float)) and rate > 0 else None


def note_regen_sample(m: Memory, w: WorldModel) -> str | None:
    """One window hurt in a safe zone: ``yes``, ``no``, or still measuring.

    The sample restarts when health falls or the last window seen is too far
    back (the character left the zone, or another state ran meanwhile).

    A "yes" leaves the health a tick it showed in ``heal_regen_per_tick``.

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
        m.heal_regen_per_tick = (w.health - s[1]) / max(1, w.tick - s[0])
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


def note_heal_pending(m: Memory, w: WorldModel, code: str, kind: str) -> None:
    """Remember health before a food ``Take`` or self-``Use`` (``kind`` is
    "take" or "use"), so the next observation can show what it healed (A24).

    Only while hurt: at full health nothing can heal, so nothing is learned.
    """
    if not hurt(w) or not heals(code):
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
