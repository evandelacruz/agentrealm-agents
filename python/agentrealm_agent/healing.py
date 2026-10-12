"""What heals and when (A10): food and potions, and the line below which
health is low.

Health rises only from a heal: a potion drunk, food eaten, or food eaten on
pickup (A130). The game has no natural regeneration, on safe ground or
anywhere, so nothing here waits for health to come back.

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
from .world import WorldModel

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


def held_heal(w: WorldModel) -> int:
    """The health the potions and food carried (held, stowed and armed) give
    back between them, each by its Supplies ``heal`` (A132): what the
    character can heal without buying."""
    carried = in_hand(w) + list(w.chest_supplies)
    return sum(row(s.code).heal or 0 for s in carried if heals(s.code))


def supply_matches(want: str, code: str) -> bool:
    """``code`` satisfies a want for ``want``: the same code, or any potion for a potion (A21)."""
    return want == code or (is_potion(want) and is_potion(code))


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
    is spent (``spend_potion``); above it the potion reserve is kept."""
    if w.health is None or w.max_health is None:
        return False
    return w.health <= w.max_health * low_health_share(params)


# When a carried potion is spent (A96), in the planner's words: the same rule
# ``spend_potion`` applies, so a reserve the planner buys is one the
# character keeps.
POTION_RULE = (
    "A carried potion is drunk only when health is low (at or below the low_health share of max, "
    "potion_drunk_at under State), or mid-fight when the win estimate says the drink turns the fight "
    "(to one it would take on, or would win). Otherwise a hurt character eats food and keeps its potions, "
    "so potions held are a reserve that lasts until then."
)


def low_health_line(w: WorldModel, params: Params) -> int | None:
    """The highest health ``health_low`` counts as low, or None unknown."""
    if w.max_health is None:
        return None
    return int(w.max_health * low_health_share(params))


def spend_potion(w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int], code: str) -> bool:
    """Whether a carried potion of ``code`` is worth drinking now (A96):
    health is low (``health_low``), or the drink turns the engagement under
    way (``engagement.drink_turns_fight``). Otherwise the potion stays a
    reserve. Every drink of a potion asks this."""
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


def note_heal_window(m: Memory, w: WorldModel) -> None:
    """Once per decision, whatever state runs: a full heal re-arms the
    ``heal_supplies`` ask, so the next hurt spell asks the planner again."""
    if w.health is not None and not hurt(w):
        m.heal_supplies_asked = False


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
