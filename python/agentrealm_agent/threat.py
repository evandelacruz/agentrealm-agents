"""Damage per hit per hostile type, learned from ``Damaged`` events (A6, M7).

The API serves no NPC's damage. Until a type is measured, callers assume it
hits as hard as the hardest hostile type already seen; with no measurements
yet, 2 (the most a weak hostile dealt in M0; docs/GAME_NOTES.md Combat).

A hit is keyed by the source's type code, looked up among perceived entities
of the same kind and id. A hit whose source is not perceived, or has no code,
is not recorded: there is no type to file it under. Trap (keyed by supply
code) and ``occupy`` damage are recorded but are not hostiles, so they never
raise the default for an unmeasured hostile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Assumption, not a measurement — PLAYABLE_AGENT_PLAN Health and lives.
UNMEASURED_DEFAULT = 2

TypeKey = tuple[str, str]  # (source_kind, type_code)

# Damaged.source_kind values that are hostiles, and the entity kind each names.
HOSTILE_KINDS = {"npc": "npc", "character": "character"}


@dataclass
class ThreatTable:
    """Max ``Damaged.amount`` seen per source type."""

    by_type: dict[TypeKey, int] = field(default_factory=dict)

    def measured(self, key: TypeKey | None) -> bool:
        return key in self.by_type

    def record(self, key: TypeKey, amount: int) -> None:
        if amount <= 0:
            return
        prev = self.by_type.get(key, 0)
        self.by_type[key] = max(prev, amount)

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


def absorb_damaged(table: ThreatTable, ev: dict, *views: list[Any]) -> TypeKey | None:
    """Fold one ``Damaged`` event into the table; return the key if recorded."""
    key = type_key_from_damaged(ev, *views)
    amount = damage_amount(ev)
    if key is None or amount is None or amount <= 0:
        return None
    table.record(key, amount)
    return key
