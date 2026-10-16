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

from .break_memory import choice_at, given_up_blocks
from .directives import PARAM_DEFAULTS
from .engagement import drink_turns_fight
from .executor.supply_refusals import can_try, in_hand
from .navigation import stuck as nav_stuck
from .item_table import InventorySupply, merge_heal
from .supplies import BLOCK_FOOD, heals, is_food, is_potion, kept_on_break, row
from .survival import hostiles_in_range, recently_attacked
from .world import Entity, Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .config import Policy
    from .knowledge_base import KnowledgeBase
    from .memory import Memory


def potion_count(w: WorldModel) -> int:
    """Held, stowed and armed potions (Boss preconditions, Shop reserve, A21,
    A38). The armed one counts too: ``held`` never lists the armed supply
    (Manual §11 Where your supplies are), so a drink that moved a potion into
    the slot and drank nothing is not a potion gone (A76)."""
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


def heals_carried(w: WorldModel) -> list[tuple[str, int]]:
    """Each potion and food carried (held, stowed and armed), as (code, heal),
    the largest heal first: what ``held_heal`` sums."""
    carried = in_hand(w) + list(w.chest_supplies)
    out = [(s.code, row(s.code).heal or 0) for s in carried if heals(s.code)]
    return sorted(out, key=lambda c: -c[1])


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


# When a carried potion is spent (A96, A135), in the planner's words: the
# same rule ``spend_potion`` applies, so a reserve the planner buys is one the
# character keeps.
POTION_RULE = (
    "A potion is for a fight: it is drunk only in combat or about to be (under attack, a hostile in range, "
    "a boss fight, inside a level, or a fight op next on the stack), when health is low (at or below the low_health "
    "share of max, potion_drunk_at under State) or the win estimate says the drink turns the fight (to one it would "
    "take on, or would win). Out of combat, lost health comes back from food: food lying near, then a bush cut for "
    "berries (State food_near), which Heal eats whenever hurt; a potion is drunk there only at the low_health line "
    "when no food near can be had. So potions held are a reserve for fights and levels, and a use_item of a potion "
    "out of combat is refused while food near can be had or health is above the low_health line."
)


def low_health_line(w: WorldModel, params: Params) -> int | None:
    """The highest health ``health_low`` counts as low, or None unknown."""
    if w.max_health is None:
        return None
    return int(w.max_health * low_health_share(params))


def in_combat(w: WorldModel, m: Memory, policy: Policy, *, fight_ahead: bool = False) -> bool:
    """In a fight or about to be one (A135): inside a level, a boss fight
    on, a hostile in range, hit by one lately (``survival.recently_attacked``),
    or a fight next on the plan (``fight_ahead``). Only then is a potion worth
    more than food."""
    if (w.map_level or 0) > 0 or fight_ahead:
        return True
    if m.boss is not None or w.in_boss_fight():
        return True
    return bool(hostiles_in_range(w, policy)) or recently_attacked(w)


def spend_potion(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    params: dict[str, float | int],
    code: str,
    *,
    food: bool = False,
    ordered: bool = False,
    fight_ahead: bool = False,
) -> bool:
    """Whether a carried potion of ``code`` is worth drinking now (A96,
    A135). Every drink of a potion asks this: Heal's (with Flee cornered and
    Retreat losing ground) and the planner's ``use_item``.

    In combat or about to be (``in_combat``, ``fight_ahead``: a fight next on
    the plan): health is low (``health_low``), the planner ordered the drink
    (``ordered``: its call on the fight stands), or the drink turns the
    engagement under way (``engagement.drink_turns_fight``). Out of combat:
    only at the low line with no food near to go for (``food``: Heal's food
    route, ``states.food.food_route``), since food heals there. Heal asks
    only once it could act on no food, so it passes no ``food``. Otherwise
    the potion stays a reserve."""
    if not in_combat(w, m, policy, fight_ahead=fight_ahead):
        return health_low(w, params) and not food
    if ordered or health_low(w, params):
        return True
    r = row(code)
    heal = r.heal if r is not None and r.heal else (w.max_health or 0)  # unknown: counted as a full heal
    return drink_turns_fight(w, m, policy, params, heal)


def carried_heal(
    w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int]
) -> InventorySupply | None:
    """Carried food first, then a potion (PLAYABLE_AGENT_PLAN.md Heal row),
    a potion only when ``spend_potion`` says it is worth drinking now.
    The armed supply counts too: ``held`` never lists it (Manual §11 Where your
    supplies are), and a potion a drink left armed is still one to drink (A76).
    Food is eaten on pickup and never carried (Manual §11 Food and potions), so
    in play the food branch never fires; it goes with A155."""
    usable = [h for h in in_hand(w) if can_try(m, w, "use", h.id)]
    for h in usable:
        if is_food(h.code):
            return h
    # Each code heals its own amount, so each is asked: a bigger heal may turn a fight a smaller one does not.
    for h in usable:
        if is_potion(h.code) and spend_potion(w, m, policy, params, h.code):
            return h
    return None


def food_to_take(w: WorldModel, m: Memory) -> list[Entity]:
    """Food lying in sight that may be taken now: one rule for Heal's pick
    and State's ``eat`` (A130). A ``Take`` the refusal table holds
    (``supply_refusals.can_try``) or a walk to it still backed off
    (``heal_food``, A90) rules it out."""
    return [
        e
        for e in w.entities
        if e.kind == "supply"
        and is_food(e.code)
        and can_try(m, w, "take", e.id, e.pos)
        and not nav_stuck.backed_off(m, "heal_food", w.map_id, e.pos, w.tick)
    ]


# A short walk for food (cells, A135): food lying this near, or a block this
# near that drops food (``supplies.BLOCK_FOOD``), heals out of a fight ahead
# of any potion. Walking further for food is a planner op (``fetch_item``).
FOOD_REACH = 3


def walk_seconds(w: WorldModel, at: Pos) -> int:
    """The straight walk to ``at`` in seconds at the character's speed, a rough estimate."""
    if w.pos is None:
        return 0
    return round(chebyshev(w.pos, at) * 1000 / max(1, w.movement_speed))


def food_blocks(w: WorldModel, m: Memory, knowledge: KnowledgeBase | None) -> list[Pos]:
    """The blocks in sight that drop food when broken (``supplies.BLOCK_FOOD``:
    a bush, for berries) and that a held item opens without being used up
    (``break_memory.choice_at``, ``supplies.kept_on_break``: a blade, never
    a match), not given up (A90)."""
    skip = given_up_blocks(m.nav_stuck, w.map_id, w.tick)
    out = []
    for p, block in w.view.tiles.items():
        if block not in BLOCK_FOOD:
            continue
        choice = choice_at(w, knowledge, p, skip)
        if choice is not None and kept_on_break(choice.supply.code):
            out.append(p)
    return out


def food_near(w: WorldModel, m: Memory, knowledge: KnowledgeBase | None) -> tuple[list[Entity], list[Pos]]:
    """The food candidates a short walk away (A135): food that may be taken
    (``food_to_take``) and blocks that drop food (``food_blocks``), each
    within ``FOOD_REACH``. Heal prices these by its walk and goes for the
    cheapest (``states.food.food_route``), which is the one food test for
    Heal, its potion and the planner's ``use_item``; State ``food_near``
    shows them."""
    here = w.pos
    if here is None:
        return [], []
    foods = [e for e in food_to_take(w, m) if chebyshev(e.pos, here) <= FOOD_REACH]
    blocks = [p for p in food_blocks(w, m, knowledge) if chebyshev(p, here) <= FOOD_REACH]
    return foods, blocks


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
