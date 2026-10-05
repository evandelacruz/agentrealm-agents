"""M9 acceptance metrics (A29): every reachable entrance mark looked, then town.

The M9 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks:

- Visits every entrance mark within its strength: every minimap entrance row
  on a non-level-interior map, except a cell the strength bracket closed
  under ``over_strength_ceiling`` for this loadout, must be looked.
- Records what each needs: ``looked`` with a ``block_type``; a locked mark
  also has ``needs: key`` (Manual §9.2, §11).
- Returns to town: the character stands on the town cell from the knowledge
  base when the run ends.

Survival gates match M7 (``acceptance_survival.py``): death, retreat miss,
unsafe Recover, loops and API errors fail immediately. Heal actions are
reported, not gated.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance_survival import SurvivalAcceptanceMetrics
from .config import Policy
from .knowledge_base import KnowledgeBase
from .knowledge_maps import is_level_interior
from .memory import Memory
from .travel.knowledge import iter_entrances, town_from_kb
from .travel.strength import StrengthBracket
from .world import Pos, WorldModel

TARGET_SECONDS = 7200.0


def required_entrance_marks(
    kb: KnowledgeBase | None,
    bracket: StrengthBracket,
) -> set[tuple[int, Pos]]:
    """Entrance marks the run must look at (M9 done-when, within strength)."""
    if kb is None:
        return set()
    out: set[tuple[int, Pos]] = set()
    for map_id, pos, _row in iter_entrances(kb):
        if is_level_interior(kb, map_id):
            continue
        if (map_id, pos) in bracket.closed:
            continue
        out.add((map_id, pos))
    return out


def entrance_row_recorded(row: dict) -> bool:
    """True when Investigate finished the look and filed what the cell needs."""
    if not row.get("looked"):
        return False
    if not isinstance(row.get("block_type"), str):
        return False
    if row.get("locked") and row.get("needs") != "key":
        return False
    return True


def missing_entrance_marks(
    kb: KnowledgeBase | None,
    bracket: StrengthBracket,
) -> list[tuple[int, Pos]]:
    """Required marks that are not recorded yet."""
    if kb is None:
        return []
    required = required_entrance_marks(kb, bracket)
    missing: list[tuple[int, Pos]] = []
    with kb.lock:
        for map_id, pos in sorted(required):
            row = kb.entrances.get(f"{map_id}:{pos[0]},{pos[1]}", {})
            if not isinstance(row, dict) or not entrance_row_recorded(row):
                missing.append((map_id, pos))
    return missing


@dataclass(kw_only=True)
class M9AcceptanceMetrics(SurvivalAcceptanceMetrics):
    """Tracks entrance looks, town return and survival while the runner plays.

    ``stop`` is set once every required entrance is recorded and the character
    stands on town, or when ``target_seconds`` have passed with the character
    alive (whichever comes first).
    """

    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    catalog_size: int = 0
    entrances_recorded: int = 0
    strength_closed_skipped: int = 0
    at_town_end: bool = False
    _catalog_ready: bool = False

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        if self.stop is not None and alive and now - self.started_at >= self.target_seconds:
            self.stop.set()

    def on_death(self) -> None:
        super().on_death()
        if self.stop is not None:
            self.stop.set()

    def before_tick(
        self,
        w: WorldModel,
        m: Memory,
        *,
        state: str,
        reason: str,
        intents: list[dict] | None,
        policy: Policy,
        params: dict[str, float | int],
        knowledge: KnowledgeBase | None,
    ) -> None:
        self.note_survival_tick(
            w,
            m,
            state=state,
            reason=reason,
            intents=intents,
            policy=policy,
            params=params,
            knowledge=knowledge,
        )
        self._refresh_entrances(knowledge, m.strength)
        town = town_from_kb(knowledge)
        if town is not None and w.map_id == town[0] and w.pos == town[1]:
            self.at_town_end = True
            if self._catalog_ready and self.entrances_recorded >= self.catalog_size and self.catalog_size:
                if self.stop is not None:
                    self.stop.set()

    def _refresh_entrances(self, kb: KnowledgeBase | None, bracket: StrengthBracket) -> None:
        if kb is None:
            return
        required = required_entrance_marks(kb, bracket)
        if not required and not self._catalog_ready:
            return
        all_marks = {(mid, pos) for mid, pos, _ in iter_entrances(kb) if not is_level_interior(kb, mid)}
        self.strength_closed_skipped = len(all_marks - required)
        self.catalog_size = len(required)
        recorded = 0
        with kb.lock:
            for map_id, pos in required:
                row = kb.entrances.get(f"{map_id}:{pos[0]},{pos[1]}", {})
                if isinstance(row, dict) and entrance_row_recorded(row):
                    recorded += 1
        self.entrances_recorded = recorded
        self._catalog_ready = True

    def entrances_ok(self) -> bool:
        return self.catalog_size > 0 and self.entrances_recorded >= self.catalog_size

    def failures(self, *, full_run: bool = True) -> list[str]:
        out = list(self.survival_failures())
        if full_run:
            if not self.catalog_size:
                out.append("no entrance catalog from minimap")
            elif not self.entrances_ok():
                missing = self.catalog_size - self.entrances_recorded
                out.append(f"{missing} entrance mark(s) not looked and recorded")
            if not self.at_town_end:
                out.append("character not on town at end")
        return out

    def summary_lines(self) -> list[str]:
        lines = [
            f"entrance marks required: {self.catalog_size} (skipped as over-strength: {self.strength_closed_skipped})",
            f"entrance marks recorded: {self.entrances_recorded}",
            f"on town at end: {self.at_town_end}",
        ]
        lines.extend(self.survival_summary_lines())
        return lines
