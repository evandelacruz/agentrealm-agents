"""Strength bracket from hunting-ground probes (A27, PLAYABLE_AGENT_PLAN Combat)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..world import Pos, WorldModel

MapPos = tuple[int, Pos]


@dataclass
class StrengthBracket:
    """Our strength is only bracketed, never read (PLAN.md Server gaps).

    Only the lower bound is kept: an ``over_strength_ceiling`` rejection means
    strength is above that ceiling. A successful entry (strength at or below
    a ceiling) would not change which grounds are candidates, so it is not
    tracked until win estimates choose between ceilings (M8).
    """

    above: int | None = None  # strength is strictly above this ceiling
    closed: set[MapPos] = field(default_factory=set)  # cells refused over_strength_ceiling under this loadout

    def note_over(self, cell: MapPos, ceiling: int | None) -> None:
        self.closed.add(cell)
        if ceiling is not None:
            self.above = ceiling if self.above is None else max(self.above, ceiling)

    def reset(self) -> set[MapPos]:
        """Forget the bracket after a loadout change; returns the cells it closed."""
        reopened, self.closed = self.closed, set()
        self.above = None
        return reopened

    def can_enter_ceiling(self, ceiling: int | None) -> bool:
        if ceiling is None:
            return True
        return self.above is None or ceiling > self.above


def loadout_key(w: WorldModel) -> tuple:
    return (w.armed_code, tuple(sorted(w.worn_codes.items())))
