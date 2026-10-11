"""Heal-state helpers: food, potions, safe-zone regen (A10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from .executor.pacing import DEFAULT_WEAPON_COOLDOWN_TICKS
from .item_table import InventorySupply, merge_heal
from .supplies import heals, is_food, is_potion
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import known_safe, safe_tiles

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase
    from .memory import Memory

# Food and potions are the Supplies reference's (A54, ``supplies.is_food``,
# ``supplies.is_potion``). Heal still tries food both ways, a ``Take`` and a
# carried ``Use``, and files what each did in the item table (A24).


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


def hurt(w: WorldModel) -> bool:
    if w.health is None or w.max_health is None:
        return False
    return w.health < w.max_health


# Health at or below this share of max is low: only then does Heal's safe
# ground (the walk there, the rest, the regen sample) outrank a plan op in
# progress (free-play run 9: a regen measure at 9/10 held off ``travel``).
LOW_HEALTH_SHARE = 0.5


def health_low(w: WorldModel) -> bool:
    """Health is at or below ``LOW_HEALTH_SHARE`` of max."""
    if w.health is None or w.max_health is None:
        return False
    return w.health <= w.max_health * LOW_HEALTH_SHARE


# What Heal does after the server refuses a food ``Take`` or a drink, by the
# rejection's code (API rules, Tick Rejection Reasons). There is no count of
# tries: each refusal changes what Heal sends next, or holds that supply until
# the situation it was refused in changes, so the same refusal is never sent
# twice into the same situation.
#
#   arm     a drink's ``Use`` found nothing armed: the next drink sends ``Arm`` first
#   forget  the supply is gone, or we do not carry it: never again
#   walk    a ``Take`` out of reach: walk onto the food, then ``Take``
#   wait    we, or the world, cannot act now: try again ``REFUSAL_WAIT_TICKS`` later
#   hold    anything else, an unknown code included: not again until the
#           situation (``heal_situation``) changes
REARM_CODES = frozenset({"nothing_armed", "not_held"})
GONE_CODES = frozenset({"supply_gone"})
REACH_CODES = frozenset({"target_not_nearby", "target_out_of_range"})
# A "wait" refusal is sent again no sooner than this: one weapon cooldown (1 s
# at 10 Hz), the longest blocker a transient code names for a Use, so a
# blocker that persists costs one refused intent a second, not one a tick.
REFUSAL_WAIT_TICKS = DEFAULT_WEAPON_COOLDOWN_TICKS
# Codes whose handling is "hold" by what they mean (the situation they need
# changed is in ``heal_situation``), so the trace does not call them unknown.
HOLD_CODES = frozenset({"would_strand", "carry_capacity_full", "not_allowed_in_safe_zone"})


# What Heal does after a drink that applied but drank nothing (A76): the
# supply is still carried and no ``SupplyUsed`` of ours named it. The cause is
# read from the result and the next observation (``noop_drink_cause``):
#
#   not_armed    the armed slot holds something else: the next drink sends
#                its ``Arm`` ("arm"), or holds when that queue already did
#   full_health  health was full when it ran: drink again only once health
#                is below what it was then ("full")
#   no_change    armed, hurt, and nothing changed: hold until the drink's
#                situation (``drink_situation``) changes or health falls
#                below what it was then
#
# A hold never outlasts a fall in health: a potion that did nothing at 8/10
# is tried again at 7/10, so it is never unusable while health falls (A67
# run 5 died at 2/10 holding two potions written off).
#
# The drink always targets ``{"kind": "self"}`` (A24), so a wrong target,
# free-play run 4's cause, is not one left to find.
NoopCause = Literal["not_armed", "full_health", "no_change"]
NOOP_CAUSES: tuple[NoopCause, ...] = ("not_armed", "full_health", "no_change")


RefusalAction = Literal["arm", "forget", "walk", "wait", "hold"]
NoopAction = Literal["arm", "hold", "full"]
# (map, cell, armed code, held supply ids, max health, the food's cell for a Take)
Situation = tuple[int | None, Pos | None, str | None, tuple[int, ...], int | None, Pos | None]
# (map, every supply id we carry, max health)
DrinkSituation = tuple[int | None, tuple[int, ...], int | None]


@dataclass(frozen=True)
class HealRefusal:
    """The last refusal of one supply's ``Take`` or drink (A80)."""

    code: str
    action: RefusalAction
    tick: int
    situation: Situation  # ``heal_situation`` when it was refused


@dataclass(frozen=True)
class NoopDrink:
    """A drink of one supply that applied and drank nothing (A76), filed
    beside A80's refusals under the same key."""

    cause: NoopCause
    action: NoopAction
    tick: int
    situation: DrinkSituation  # ``drink_situation`` when it was filed
    health: int | None  # health when it was filed: a drink waits for less


@dataclass
class AppliedDrink:
    """Heal's drink whose ``Use`` applied, kept until the next decision reads
    from the observation whether a supply went (A76)."""

    supply_id: int
    code: str | None
    tick: int
    armed_first: bool  # its queue sent the supply's ``Arm`` before the ``Use``
    health: int | None  # health before the response that carried its result
    used: bool = False  # our own ``SupplyUsed`` named its code


def heal_situation(w: WorldModel, target: Pos | None = None) -> Situation:
    """What a ``Take`` or drink was decided from: where we stand, what is armed
    and held, max health, and the food's cell for a ``Take``. A held supply is
    tried again only once one of these differs (a step, a new item, a new
    max health, the food moved).

    Health itself is left out: regen, poison or a hit moves it every few
    ticks without touching anything a refusal depends on, and would resend
    the same refusal each time.
    """
    held = tuple(sorted(h.id for h in w.held_supplies))
    return (w.map_id, w.pos, w.armed_code, held, w.max_health, target)


def carried_ids(w: WorldModel) -> tuple[int, ...]:
    """Every supply id we carry: held, stowed in the carried chest, and armed."""
    ids = {h.id for h in w.held_supplies} | {s.id for s in w.chest_supplies}
    if w.armed_id is not None:
        ids.add(w.armed_id)
    return tuple(sorted(ids))


def drink_situation(w: WorldModel) -> DrinkSituation:
    """What a drink that drank nothing is held against (A76): the map and what
    we carry, by id, and max health. Not the cell or the armed slot: a walk
    and the re-arm after every drink change those without changing what the
    drink does, and would resend it each time. Health is checked apart, by
    ``can_try``: only a fall below the filed reading counts."""
    return (w.map_id, carried_ids(w), w.max_health)


def drank(events: list[dict], character_id: int | None, code: str | None) -> bool:
    """Our own ``SupplyUsed`` for ``code`` is among ``events``: it was used up.
    The event reaches every character in sight, so another's is not ours."""
    return any(
        e.get("kind") == "SupplyUsed" and e.get("actor_id") == character_id and e.get("supply_code") == code
        for e in events
    )


def noop_drink_cause(w: WorldModel, d: AppliedDrink) -> NoopCause | None:
    """Why an applied drink drank nothing, from the observation after it
    (``NOOP_CAUSES``), or None when it went through: our ``SupplyUsed``
    named it, we no longer carry it, or health rose."""
    if d.used or d.supply_id not in carried_ids(w):
        return None
    if w.health is not None and d.health is not None and w.health > d.health:
        return None
    in_slot = w.armed_id == d.supply_id if w.armed_id is not None else w.armed_code == d.code
    if not in_slot:
        return "not_armed"
    if w.health is not None and w.max_health is not None and w.health >= w.max_health:
        return "full_health"
    return "no_change"


def noop_action(cause: NoopCause, *, armed_first: bool) -> NoopAction:
    """What to do about a drink that drank nothing (see ``NOOP_CAUSES``)."""
    if cause == "not_armed":
        return "hold" if armed_first else "arm"
    if cause == "full_health":
        return "full"
    return "hold"


def refusal_action(rejection: dict, *, verb: str, armed_first: bool, on_target: bool) -> RefusalAction:
    """What to do about one refusal (see ``REARM_CODES`` and the table above).

    ``verb`` is the refused intent's. ``armed_first`` says the drink already
    sent its ``Arm``, so arming again is not something new to try, and
    ``on_target`` that a ``Take`` was sent standing on the food, so walking
    there is not either: both then hold.
    """
    code = rejection.get("code") or ""
    if code in GONE_CODES or (code == "not_held" and verb in ("Arm", "Take")):
        return "forget"
    if code in REARM_CODES and verb == "Use":
        return "hold" if armed_first else "arm"
    if code in REACH_CODES and verb == "Take":
        return "hold" if on_target else "walk"
    # Retryability first, as the API says: ``character_ended`` and a closed
    # world's ``world_not_open`` are ``state`` codes that are ``permanent``.
    if rejection.get("retryability") == "permanent":
        return "forget"
    if rejection.get("retryability") == "transient" or rejection.get("category") == "state":
        return "wait"
    return "hold"


def known_refusal(rejection: dict) -> bool:
    """A code this module handles by name; any other is traced as unknown."""
    code = rejection.get("code") or ""
    return code in REARM_CODES | GONE_CODES | REACH_CODES | HOLD_CODES or rejection.get("category") == "state"


def note_refusal(m: Memory, kind: str, supply_id: int, refusal: HealRefusal | NoopDrink) -> None:
    """File a refusal of ``supply_id`` (``kind`` is "take" or "use"). Only the
    runner calls it, for a result the server sent, never a probe (A77)."""
    m.heal_refusals[(kind, supply_id)] = refusal


def clear_refusal(m: Memory, kind: str, supply_id: int) -> None:
    """The ``Take`` or drink applied: what was refused before is over. A drink
    that drank nothing is filed again from the next observation (A76)."""
    m.heal_refusals.pop((kind, supply_id), None)


def can_try(m: Memory, w: WorldModel, kind: str, supply_id: int, target: Pos | None = None) -> bool:
    """Whether Heal may send a ``Take`` (``kind`` "take", ``target`` the food's
    cell) or a drink ("use") of ``supply_id`` now, by its last refusal."""
    r = m.heal_refusals.get((kind, supply_id))
    if r is None or r.action in ("arm", "walk"):
        return True
    if isinstance(r, NoopDrink):
        fell = w.health is not None and r.health is not None and w.health < r.health
        return fell or (r.action == "hold" and drink_situation(w) != r.situation)
    if r.action == "forget":
        return False
    if r.action == "wait":
        return w.tick >= r.tick + REFUSAL_WAIT_TICKS and w.alive
    return heal_situation(w, target) != r.situation


def must_arm(m: Memory, supply_id: int) -> bool:
    """A drink of ``supply_id`` found nothing armed, or applied with something
    else in the slot: send its ``Arm`` whatever ``armed_code`` says (it may
    trail the server)."""
    r = m.heal_refusals.get(("use", supply_id))
    return r is not None and r.action == "arm"


def must_stand_on(m: Memory, food: Entity) -> bool:
    """A ``Take`` of ``food`` was out of reach from where it was sent: walk onto
    its cell before taking it. Food that moved since is aimed at afresh."""
    r = m.heal_refusals.get(("take", food.id))
    return isinstance(r, HealRefusal) and r.action == "walk" and r.situation[-1] == food.pos


def food_in_sight(w: WorldModel, m: Memory) -> list[Entity]:
    here = w.pos
    if here is None:
        return []
    out = [e for e in w.entities if e.kind == "supply" and is_food(e.code) and can_try(m, w, "take", e.id, e.pos)]
    out.sort(key=lambda e: (chebyshev(e.pos, here), e.id))
    return out


def carried_heal(w: WorldModel, m: Memory) -> InventorySupply | None:
    """Carried food first, then a potion (PLAYABLE_AGENT_PLAN.md Heal row).
    The armed supply counts too: the snapshot may leave it out of ``held``
    (GAME_NOTES open questions), and a potion a drink left armed is still one
    to drink (A76)."""
    carried = list(w.held_supplies)
    if w.armed_id is not None and w.armed_code is not None and all(h.id != w.armed_id for h in carried):
        carried.append(InventorySupply(w.armed_id, w.armed_code))
    usable = [h for h in carried if can_try(m, w, "use", h.id)]
    for kind in (is_food, is_potion):
        for h in usable:
            if kind(h.code):
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
    ``heal_supplies`` ask, so the next hurt spell asks the planner again, and
    lets the next hurt spell walk to safe ground again (``heal_safe_given_up``)."""
    if w.health is not None and not hurt(w):
        m.heal_supplies_asked = False
        m.heal_safe_given_up = None


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


def code_in_hand(w: WorldModel, queue: list[dict], index: int) -> str | None:
    """The code the ``Use`` at ``queue[index]`` was made with, from the last
    intent other than a ``Wait`` queued before it: what a self-``Use`` drinks
    or eats, or what a cut cut with.

    Heal and Gather send ``[Arm item, Wait…, Use]`` in one queue, and its
    results are applied before the observation updates ``armed_code``. So an
    ``Arm`` before the ``Use`` names the item while it is still held; with
    none, or once an observation shows it armed, it is ``armed_code``.
    """
    before = next((i for i in reversed(queue[:index]) if i.get("verb") != "Wait"), None)
    if before and before.get("verb") == "Arm":
        for h in w.held_supplies:
            if h.id == before.get("supply_id"):
                return h.code
    return w.armed_code


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
