"""Damage per hit per hostile type, learned from ``Damaged`` events (A6, M7).

The API serves no NPC's damage. Until a type is measured, callers assume it
hits as hard as the hardest type already seen; with no measurements yet, 2
(the most a weak hostile dealt in M0; docs/GAME_NOTES.md Combat).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Assumption, not a measurement — PLAYABLE_AGENT_PLAN Health and lives.
UNMEASURED_DEFAULT = 2

TypeKey = tuple[str, str]  # (source_kind, type_code)


@dataclass
class ThreatTable:
    """Max ``Damaged.amount`` seen per hostile type."""

    by_type: dict[TypeKey, int] = field(default_factory=dict)

    def measured(self, key: TypeKey) -> bool:
        return key in self.by_type

    def record(self, key: TypeKey, amount: int) -> None:
        if amount <= 0:
            return
        prev = self.by_type.get(key, 0)
        self.by_type[key] = max(prev, amount)

    def damage_per_hit(self, key: TypeKey) -> int:
        """Expected damage for one hit from this type (conservative if unknown)."""
        if key in self.by_type:
            return self.by_type[key]
        if self.by_type:
            return max(self.by_type.values())
        return UNMEASURED_DEFAULT


def type_key_from_damaged(ev: dict, entities: list[Any]) -> TypeKey | None:
    """Map a ``Damaged`` event to a stable hostile-type key."""
    kind = ev.get("source_kind")
    if not kind:
        return None
    if kind == "occupy":
        return ("occupy", "occupy")
    if kind == "trap":
        sid = ev.get("source_id")
        if sid is not None:
            for e in entities:
                if e.kind == "supply" and e.id == sid:
                    return ("trap", e.code or f"supply:{sid}")
        return ("trap", "trap")
    sid = ev.get("source_id")
    if sid is None:
        return (kind, kind)
    for e in entities:
        if e.id != sid:
            continue
        if e.kind == "npc":
            return ("npc", e.code or f"npc:{sid}")
        if e.kind == "character":
            return ("character", e.code or f"character:{sid}")
        if e.kind == "supply":
            return ("trap", e.code or f"supply:{sid}")
    return (kind, f"id:{sid}")


def type_key_for_entity(e: Any) -> TypeKey | None:
    """Type key for a perceived hostile entity."""
    if e.kind == "npc":
        return ("npc", e.code or f"npc:{e.id}")
    if e.kind == "character":
        return ("character", e.code or f"character:{e.id}")
    return None


def absorb_damaged(table: ThreatTable, ev: dict, entities: list[Any]) -> TypeKey | None:
    """Fold one ``Damaged`` event into the table; return the key if recorded."""
    key = type_key_from_damaged(ev, entities)
    if key is None:
        return None
    try:
        amount = int(ev.get("amount", 0))
    except (TypeError, ValueError):
        return None
    table.record(key, amount)
    return key
