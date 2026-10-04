"""Strength bracket from hunting-ground probes (A27, PLAYABLE_AGENT_PLAN Combat)."""

from __future__ import annotations

from dataclasses import dataclass

from ..world import Pos, WorldModel


@dataclass
class StrengthBracket:
    """Our strength is only bracketed, never read (PLAN.md server gaps)."""

    above: int | None = None  # strength is strictly above this ceiling
    at_or_below: int | None = None  # strength is at or below this ceiling

    def reset(self) -> None:
        self.above = self.at_or_below = None

    def can_enter_ceiling(self, ceiling: int | None) -> bool:
        if ceiling is None:
            return True
        if self.above is not None and ceiling <= self.above:
            return False
        return True


def loadout_key(w: WorldModel) -> tuple:
    return (w.armed_code, tuple(sorted(w.worn_codes.items())))


def note_over_strength(bracket: StrengthBracket, ceiling: int | None) -> None:
    if ceiling is None:
        return
    bracket.above = ceiling if bracket.above is None else max(bracket.above, ceiling)


def note_hunting_entry(bracket: StrengthBracket, w: WorldModel, map_id: int, pos: Pos) -> None:
    fact = w.zones.get(map_id, {}).get(pos)
    if fact is None or fact.strength_ceiling is None:
        return
    c = fact.strength_ceiling
    bracket.at_or_below = c if bracket.at_or_below is None else min(bracket.at_or_below, c)
