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

import statistics
from typing import TYPE_CHECKING

from .directives import PARAM_DEFAULTS
from .engagement import drink_turns_fight
from .executor.supply_refusals import can_try, in_hand
from .item_table import InventorySupply, merge_heal
from .memory import RegenStretch
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


def held_heal(w: WorldModel) -> int:
    """The health the potions and food carried (held, stowed and armed) give
    back between them, each by its Supplies ``heal`` (A132): what the
    character can heal without buying or resting."""
    carried = in_hand(w) + list(w.chest_supplies)
    return sum(row(s.code).heal or 0 for s in carried if heals(s.code))


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
# Safe-zone regen's rate (A124): the health a tick of each stretch timed
# from one rise to the next, the newest REGEN_RATES_KEPT; the estimate is
# their median, so one bad stretch cannot set it. Rough: a decision sees a
# rise up to a decision late.
REGEN_RATES_KEY = "safe_zone_regen_rates"
REGEN_RATES_KEPT = 9


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


def save_regen_yes(knowledge: KnowledgeBase | None) -> None:
    """Health came back in a safe zone: a fact for every run on this world."""
    if knowledge is None:
        return
    with knowledge.lock:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        if not isinstance(surv, dict):
            knowledge.extra[SURVIVAL_KEY] = surv = {}
        surv[REGEN_KEY] = "yes"


def regen_per_tick(knowledge: KnowledgeBase | None) -> float | None:
    """The estimate of safe-zone regen on this world, health a tick: the
    median of the stretches measured, and only once regen is a confirmed
    "yes". None otherwise: the published docs give health back only from a
    heal or a respawn, so with none confirmed a rest heals nothing (A124)."""
    if knowledge is None:
        return None
    with knowledge.lock:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        if not isinstance(surv, dict) or surv.get(REGEN_KEY) != "yes":
            return None
        rates = [r for r in surv.get(REGEN_RATES_KEY) or [] if isinstance(r, (int, float)) and r > 0]
    return statistics.median(rates) if rates else None


def note_regen_rate(m: Memory, w: WorldModel, knowledge: KnowledgeBase | None) -> None:
    """One of Heal's rest decisions, hurt in a safe zone: time regen between
    rises (A124). A stretch from one rise to the next spans whole regen
    periods, wherever the rest began; each adds its rate (``add_regen``) and
    the later rise starts the next. Only rest decisions back to back count:
    any other decision between them (another state ran, or Heal ate or
    drank), health that falls, a change of max health, or any heal sent
    (``note_heal_pending``: a potion or food Use, a food Take, from any
    state) ends the stretch, and timing starts again at the next rise."""
    s, health, decision = m.heal_regen_stretch, w.health, m.nav_stuck.decision
    if health is None:
        m.heal_regen_stretch = None
        return
    if s is None or decision != s.decision + 1 or health < s.health or w.max_health != s.max_health:
        m.heal_regen_stretch = RegenStretch(decision, health, w.max_health)
        return
    if health > s.health:
        if s.rise_tick is not None and s.rise_health is not None and w.tick > s.rise_tick:
            add_regen(knowledge, (health - s.rise_health) / (w.tick - s.rise_tick))
        s.rise_tick, s.rise_health = w.tick, health
    s.decision, s.health = decision, health


def add_regen(knowledge: KnowledgeBase | None, per_tick: float) -> None:
    """One stretch's regen, health a tick, kept with the newest
    ``REGEN_RATES_KEPT`` on this world (A124)."""
    if knowledge is None or per_tick <= 0:
        return
    with knowledge.lock:
        surv = knowledge.extra.get(SURVIVAL_KEY)
        if not isinstance(surv, dict):
            knowledge.extra[SURVIVAL_KEY] = surv = {}
        rates = [r for r in surv.get(REGEN_RATES_KEY) or [] if isinstance(r, (int, float))]
        surv[REGEN_RATES_KEY] = [*rates, per_tick][-REGEN_RATES_KEPT:]


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


def note_heal_pending(m: Memory, w: WorldModel, code: str, kind: str) -> None:
    """Remember health before a food ``Take`` or self-``Use`` (``kind`` is
    "take" or "use"), so the next observation can show what it healed (A24).

    Only while hurt: at full health nothing can heal, so nothing is learned.
    """
    if not hurt(w) or not heals(code):
        return
    assert w.health is not None
    m.heal_pending = (w.health, code, kind)
    # A heal is no regen: it ends both the yes/no sample and the stretch timed (A124).
    m.heal_regen_sample = m.heal_regen_stretch = None


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
