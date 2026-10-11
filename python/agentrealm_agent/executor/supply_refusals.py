"""What is sent next after the server refuses a food ``Take`` or a ``Use`` on
yourself, or a ``Use`` on yourself applies and does nothing (A80, A76).

One table from a refusal's API code to an action (``refusal_action``), one
from a no-effect cause to an action (``noop_action``), and one test of
whether a supply may be tried now (``can_try``). The runner files what the
server said (``Runner.file_supply_refusals``), never what a held-queue probe
only decided (A77); states only ask ``can_try``, ``must_arm`` and
``must_stand_on``. There is no count of tries: each refusal changes what is
sent next, or holds that supply until the situation it was refused in
changes, so the same refusal is never sent twice into the same situation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..item_table import InventorySupply
from ..supplies import heals
from ..world import Entity, Pos, WorldModel
from .pacing import DEFAULT_WEAPON_COOLDOWN_TICKS

if TYPE_CHECKING:
    from ..memory import Memory

# What is sent after a refusal, by the rejection's code (API rules, Tick
# Rejection Reasons):
#
#   arm     a ``Use`` on yourself found nothing armed: the next one sends ``Arm`` first
#   forget  the supply is gone, or we do not carry it: never again
#   walk    a ``Take`` out of reach: walk onto the food, then ``Take``
#   wait    we, or the world, cannot act now: try again ``REFUSAL_WAIT_TICKS`` later
#   hold    anything else, an unknown code included: not again until the
#           situation (``refusal_situation``) changes
REARM_CODES = frozenset({"nothing_armed", "not_held"})
GONE_CODES = frozenset({"supply_gone"})
REACH_CODES = frozenset({"target_not_nearby", "target_out_of_range"})
# A "wait" refusal is sent again no sooner than this: one weapon cooldown (1 s
# at 10 Hz), the longest blocker a transient code names for a Use, so a
# blocker that persists costs one refused intent a second, not one a tick.
REFUSAL_WAIT_TICKS = DEFAULT_WEAPON_COOLDOWN_TICKS
# Codes whose handling is "hold" by what they mean (the situation they need
# changed is in ``refusal_situation``), so the trace does not call them unknown.
HOLD_CODES = frozenset({"would_strand", "carry_capacity_full", "not_allowed_in_safe_zone"})


# Why a ``Use`` on yourself that applied did nothing: the supply is still
# carried, no ``SupplyUsed`` of ours named it, and its effect did not show
# (``noop_cause``, read from the observation after it):
#
#   not_armed    the armed slot holds something else: the next one sends
#                its ``Arm`` ("arm"), or holds when that queue already did
#   full_health  what it raises was already at its max: try again only once
#                that falls below where it was then ("full")
#   no_change    armed, room to raise, and nothing changed: hold until the
#                situation (``noop_situation``) changes or what it raises
#                falls below where it was then
#
# A hold never outlasts a fall in what the item raises: a potion that did
# nothing at 8/10 is tried again at 7/10, so a potion is never written off
# while health falls. The ``Use`` always targets ``{"kind": "self"}`` (A24),
# so a wrong target is not a cause left to find.
NoopCause = Literal["not_armed", "full_health", "no_change"]
NOOP_CAUSES: tuple[NoopCause, ...] = ("not_armed", "full_health", "no_change")


RefusalAction = Literal["arm", "forget", "walk", "wait", "hold"]
NoopAction = Literal["arm", "hold", "full"]
# (map, cell, armed code, held supply ids, max health, the food's cell for a Take)
Situation = tuple[int | None, Pos | None, str | None, tuple[int, ...], int | None, Pos | None]
# (map, every supply id we carry, max health)
NoopSituation = tuple[int | None, tuple[int, ...], int | None]


@dataclass(frozen=True)
class Refusal:
    """The last refusal of one supply's ``Take`` or ``Use`` on yourself (A80)."""

    code: str
    action: RefusalAction
    tick: int
    situation: Situation  # ``refusal_situation`` when it was refused


@dataclass(frozen=True)
class NoopUse:
    """A ``Use`` on yourself of one supply that applied and did nothing (A76),
    filed beside the refusals under the same key."""

    cause: NoopCause
    action: NoopAction
    tick: int
    situation: NoopSituation  # ``noop_situation`` when it was filed
    measure: int | None  # what the item raises (``effect_measure``) when filed: tried again below it


@dataclass
class AppliedSelfUse:
    """A ``Use`` on yourself that applied (a potion, food, a chest, A108),
    kept until the next decision reads from the observation whether its
    supply went (A76)."""

    supply_id: int
    code: str | None
    tick: int
    armed_first: bool  # its queue sent the supply's ``Arm`` before the ``Use``
    measure: int | None  # ``effect_measure`` before the response that carried its result
    used: bool = False  # our own ``SupplyUsed`` named its code


def effect_measure(w: WorldModel, code: str | None) -> int | None:
    """What a ``Use`` on yourself of ``code`` raises, as the world shows it:
    health for an item that heals. None for an effect that shows only as the
    item used up (a chest, whose slots ``loot.learn_chest_upgrade`` learns)."""
    return w.health if heals(code) else None


def effect_at_max(w: WorldModel, code: str | None) -> bool:
    """What ``code`` raises is already at its max: health full, for a heal."""
    return heals(code) and w.health is not None and w.max_health is not None and w.health >= w.max_health


def in_hand(w: WorldModel) -> list[InventorySupply]:
    """The held supplies, and the armed one: the snapshot may leave it out of
    ``held`` (GAME_NOTES open questions). What a ``Use`` on yourself can be
    sent with, after its ``Arm``."""
    carried = list(w.held_supplies)
    if w.armed_id is not None and w.armed_code is not None and all(h.id != w.armed_id for h in carried):
        carried.append(InventorySupply(w.armed_id, w.armed_code))
    return carried


def carried_ids(w: WorldModel) -> tuple[int, ...]:
    """Every supply id we carry: held, stowed in the carried chest, and armed."""
    ids = {h.id for h in w.held_supplies} | {s.id for s in w.chest_supplies}
    if w.armed_id is not None:
        ids.add(w.armed_id)
    return tuple(sorted(ids))


def refusal_situation(w: WorldModel, target: Pos | None = None) -> Situation:
    """What a ``Take`` or ``Use`` was decided from: where we stand, what is
    armed and held, max health, and the food's cell for a ``Take``. A held
    supply is tried again only once one of these differs (a step, a new item,
    a new max health, the food moved).

    Health itself is left out: regen, poison or a hit moves it every few
    ticks without touching anything a refusal depends on, and would resend
    the same refusal each time.
    """
    held = tuple(sorted(h.id for h in w.held_supplies))
    return (w.map_id, w.pos, w.armed_code, held, w.max_health, target)


def noop_situation(w: WorldModel) -> NoopSituation:
    """What a ``Use`` that did nothing is held against (A76): the map and what
    we carry, by id, and max health. Not the cell or the armed slot: a walk
    and the re-arm after every ``Use`` change those without changing what
    the ``Use`` does, and would resend it each time. What the item raises is
    checked apart, by ``can_try``: only a fall below the filed reading counts."""
    return (w.map_id, carried_ids(w), w.max_health)


def used_up(events: list[dict], character_id: int | None, code: str | None) -> bool:
    """Our own ``SupplyUsed`` for ``code`` is among ``events``: it was used up.
    The event reaches every character in sight, so another's is not ours."""
    return any(
        e.get("kind") == "SupplyUsed" and e.get("actor_id") == character_id and e.get("supply_code") == code
        for e in events
    )


def noop_cause(w: WorldModel, d: AppliedSelfUse) -> NoopCause | None:
    """Why an applied ``Use`` on yourself did nothing, from the observation
    after it (``NOOP_CAUSES``), or None when it went through: our
    ``SupplyUsed`` named it, we no longer carry it, or what it raises rose."""
    if d.used or d.supply_id not in carried_ids(w):
        return None
    now = effect_measure(w, d.code)
    if now is not None and d.measure is not None and now > d.measure:
        return None
    in_slot = w.armed_id == d.supply_id if w.armed_id is not None else w.armed_code == d.code
    if not in_slot:
        return "not_armed"
    if effect_at_max(w, d.code):
        return "full_health"
    return "no_change"


def noop_action(cause: NoopCause, *, armed_first: bool) -> NoopAction:
    """What to do about a ``Use`` that did nothing (see ``NOOP_CAUSES``)."""
    if cause == "not_armed":
        return "hold" if armed_first else "arm"
    if cause == "full_health":
        return "full"
    return "hold"


def refusal_action(rejection: dict, *, verb: str, armed_first: bool, on_target: bool) -> RefusalAction:
    """What to do about one refusal (see ``REARM_CODES`` and the table above).

    ``verb`` is the refused intent's. ``armed_first`` says the ``Use`` already
    went with its ``Arm``, so arming again is not something new to try, and
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
    """A code this table handles by name; any other is traced as unknown."""
    code = rejection.get("code") or ""
    return code in REARM_CODES | GONE_CODES | REACH_CODES | HOLD_CODES or rejection.get("category") == "state"


def note_refusal(m: Memory, kind: str, supply_id: int, refusal: Refusal | NoopUse) -> None:
    """File a refusal of ``supply_id`` (``kind`` is "take" or "use"). Only the
    runner calls it, for a result the server sent, never a probe (A77)."""
    m.supply_refusals[(kind, supply_id)] = refusal


def clear_refusal(m: Memory, kind: str, supply_id: int) -> None:
    """The ``Take`` or ``Use`` applied: what was refused before is over. A
    ``Use`` that did nothing is filed again from the next observation (A76)."""
    m.supply_refusals.pop((kind, supply_id), None)


def can_try(m: Memory, w: WorldModel, kind: str, supply_id: int, target: Pos | None = None) -> bool:
    """Whether a ``Take`` (``kind`` "take", ``target`` the food's cell) or a
    ``Use`` on yourself ("use") of ``supply_id`` may be sent now, by its last
    refusal."""
    r = m.supply_refusals.get((kind, supply_id))
    if r is None or r.action in ("arm", "walk"):
        return True
    if isinstance(r, NoopUse):
        code = next((h.code for h in in_hand(w) if h.id == supply_id), None)
        now = effect_measure(w, code)
        fell = now is not None and r.measure is not None and now < r.measure
        return fell or (r.action == "hold" and noop_situation(w) != r.situation)
    if r.action == "forget":
        return False
    if r.action == "wait":
        return w.tick >= r.tick + REFUSAL_WAIT_TICKS and w.alive
    return refusal_situation(w, target) != r.situation


def must_arm(m: Memory, supply_id: int) -> bool:
    """A ``Use`` of ``supply_id`` found nothing armed, or applied with
    something else in the slot: send its ``Arm`` whatever ``armed_code`` says
    (it may trail the server)."""
    r = m.supply_refusals.get(("use", supply_id))
    return r is not None and r.action == "arm"


def must_stand_on(m: Memory, food: Entity) -> bool:
    """A ``Take`` of ``food`` was out of reach from where it was sent: walk onto
    its cell before taking it. Food that moved since is aimed at afresh."""
    r = m.supply_refusals.get(("take", food.id))
    return isinstance(r, Refusal) and r.action == "walk" and r.situation[-1] == food.pos


def code_in_hand(w: WorldModel, queue: list[dict], index: int) -> str | None:
    """The code the ``Use`` at ``queue[index]`` was made with, from the last
    intent other than a ``Wait`` queued before it: what a self-``Use`` drinks
    or eats, or what a cut cut with.

    A ``Use`` on yourself and a cut go out as ``[Arm item, Wait…, Use]`` in
    one queue, and its results are applied before the observation updates
    ``armed_code``. So an ``Arm`` before the ``Use`` names the item while it
    is still held; with none, or once an observation shows it armed, it is
    ``armed_code``.
    """
    before = next((i for i in reversed(queue[:index]) if i.get("verb") != "Wait"), None)
    if before and before.get("verb") == "Arm":
        for h in w.held_supplies:
            if h.id == before.get("supply_id"):
                return h.code
    return w.armed_code
