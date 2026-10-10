"""Damage per hit per hostile type, learned from ``Damaged`` events (A6, M7).

The API serves no NPC's damage. Until a type is measured, callers assume it
hits as hard as the hardest hostile type already seen; with no measurements
yet, 2 (the most a weak hostile dealt in M0; docs/GAME_NOTES.md Combat).

A hit is keyed by the source's type code, looked up among perceived entities
of the same kind and id. A hit whose source is not perceived, or has no code,
is not recorded: there is no type to file it under. Trap (keyed by supply
code) and ``occupy`` damage are recorded but are not hostiles, so they never
raise the default for an unmeasured hostile.

Each hostile type's swings at us are counted too: a hit per ``Damaged``, a
miss per ``Attacked`` with no ``Damaged`` from the same attacker on its tick
(API Events, B131). ``hostile_memory`` carries the table across runs (A84).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Assumption, not a measurement — PLAYABLE_AGENT_PLAN Health and lives. It
# is also the attack power the win estimate gives a type never measured: the
# world's base (GAME_NOTES Combat); the Manual publishes no hostile's own.
UNMEASURED_DEFAULT = 2
# How many swings the published hit chance counts for against a type's
# measured hits and misses (``ThreatTable.hit_rate``): one type's rate moves
# off the roll's only once it has swung at us about this often.
PRIOR_SWINGS = 10

TypeKey = tuple[str, str]  # (source_kind, type_code)

# Damaged.source_kind values that are hostiles, and the entity kind each names.
HOSTILE_KINDS = {"npc": "npc", "character": "character"}


@dataclass
class ThreatTable:
    """Max ``Damaged.amount`` seen per source type, and per hostile type the
    swings at us that hit and missed. ``saved_hits`` and ``saved_misses``
    are the counts already in the knowledge base (``hostile_memory``)."""

    by_type: dict[TypeKey, int] = field(default_factory=dict)
    hits: dict[TypeKey, int] = field(default_factory=dict)
    misses: dict[TypeKey, int] = field(default_factory=dict)
    saved_hits: dict[TypeKey, int] = field(default_factory=dict)
    saved_misses: dict[TypeKey, int] = field(default_factory=dict)

    def measured(self, key: TypeKey | None) -> bool:
        return key in self.by_type

    def record(self, key: TypeKey, amount: int) -> None:
        if amount <= 0:
            return
        prev = self.by_type.get(key, 0)
        self.by_type[key] = max(prev, amount)

    def hit_rate(self, key: TypeKey | None, published: float) -> float:
        """This type's share of swings that hit us: its counted hits and
        misses, with the ``published`` chance counted as ``PRIOR_SWINGS``."""
        hits, misses = self.hits.get(key, 0), self.misses.get(key, 0)
        return (hits + PRIOR_SWINGS * published) / (hits + misses + PRIOR_SWINGS)

    def damage_per_hit(self, key: TypeKey | None) -> int:
        """Expected damage for one hit from this type (conservative if unknown).

        None (a hostile whose type is unknown) gets the unmeasured default.
        """
        if key in self.by_type:
            return self.by_type[key]
        hostile = [v for k, v in self.by_type.items() if k[0] in HOSTILE_KINDS]
        return max(hostile) if hostile else UNMEASURED_DEFAULT


def _find(views: Iterable[list[Any]], kind: str, eid: Any) -> Any | None:
    for entities in views:
        for e in entities:
            if e.kind == kind and e.id == eid:
                return e
    return None


def type_key_from_damaged(ev: dict, *views: list[Any]) -> TypeKey | None:
    """Map a ``Damaged`` event to a stable type key, or None if unresolvable.

    views are entity lists searched in order (the freshest first).
    """
    kind = ev.get("source_kind")
    if kind == "occupy":
        return ("occupy", "occupy")
    sid = ev.get("source_id")
    if sid is None:
        return None
    if kind == "trap":
        e = _find(views, "supply", sid)
        return ("trap", e.code) if e is not None and e.code else None
    if kind in HOSTILE_KINDS:
        e = _find(views, HOSTILE_KINDS[kind], sid)
        return type_key_for_entity(e) if e is not None else None
    return None


def hostile_type_from_event(ev: dict, *views: list[Any]) -> TypeKey | None:
    """The NPC type an event shows hostile, or None.

    An ``Attacked`` names the NPC that swung at us (``actor_kind``,
    ``actor_id``), a ``Damaged`` the one that hit us (``source_kind``,
    ``source_id``), both looked up in ``views`` like ``type_key_from_damaged``.
    An ``NPCDied`` names its type itself: only a hostile dies (API Events).
    """
    kind = ev.get("kind")
    if kind == "NPCDied":
        code = ev.get("npc_type")
        return ("npc", code) if isinstance(code, str) and code else None
    if kind == "Attacked":
        actor_kind, actor_id = ev.get("actor_kind"), ev.get("actor_id")
    elif kind == "Damaged":
        actor_kind, actor_id = ev.get("source_kind"), ev.get("source_id")
    else:
        return None
    if actor_kind != "npc" or actor_id is None:
        return None
    e = _find(views, "npc", actor_id)
    return type_key_for_entity(e) if e is not None else None


def hostile_hit(ev: dict) -> bool:
    """An ``Attacked``, or a ``Damaged`` whose source is a hostile (not a trap or ``occupy`` ground)."""
    kind = ev.get("kind")
    if kind == "Attacked":
        return True
    return kind == "Damaged" and ev.get("source_kind") in HOSTILE_KINDS


def hitter(ev: dict) -> tuple[str, Any] | None:
    """(entity kind, id) of the hostile a ``Damaged`` names as its source, or None."""
    kind = HOSTILE_KINDS.get(ev.get("source_kind"))
    sid = ev.get("source_id")
    return (kind, sid) if kind is not None and sid is not None else None


def type_key_for_entity(e: Any) -> TypeKey | None:
    """Type key for a perceived hostile entity; None when it has no type code."""
    if e.kind in HOSTILE_KINDS.values() and e.code:
        return (e.kind, e.code)
    return None


def damage_amount(ev: dict) -> int | None:
    """``Damaged.amount`` as an int, or None when it is not a number."""
    v = ev.get("amount")
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def count_swings(table: ThreatTable, events: list[dict], *views: list[Any]) -> None:
    """Count each hostile type's swings at us in one round trip's ``events``:
    a ``Damaged`` from it is a hit, an ``Attacked`` past its ``Damaged``
    count on the same tick a miss. A swing whose source is not perceived is
    not counted."""
    attacks: dict[tuple[Any, Any, Any], int] = {}
    for ev in events:
        kind = ev.get("kind")
        if kind == "Damaged" and ev.get("source_kind") in HOSTILE_KINDS:
            side = (ev.get("source_kind"), ev.get("source_id"), ev.get("tick"))
            attacks[side] = attacks.get(side, 0) - 1
            key = type_key_from_damaged(ev, *views)
            if key is not None:
                table.hits[key] = table.hits.get(key, 0) + 1
        elif kind == "Attacked" and ev.get("actor_kind") in HOSTILE_KINDS:
            side = (ev.get("actor_kind"), ev.get("actor_id"), ev.get("tick"))
            attacks[side] = attacks.get(side, 0) + 1
    for (kind, sid, _), missed in attacks.items():
        e = _find(views, HOSTILE_KINDS[kind], sid) if missed > 0 and sid is not None else None
        key = type_key_for_entity(e) if e is not None else None
        if key is not None:
            table.misses[key] = table.misses.get(key, 0) + missed


def absorb_damaged(table: ThreatTable, ev: dict, *views: list[Any]) -> TypeKey | None:
    """Fold one ``Damaged`` event into the table; return the key if recorded."""
    key = type_key_from_damaged(ev, *views)
    amount = damage_amount(ev)
    if key is None or amount is None or amount <= 0:
        return None
    table.record(key, amount)
    return key
