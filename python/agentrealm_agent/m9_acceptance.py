"""M9 acceptance metrics (A29): every reachable entrance mark looked, then town.

The M9 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks:

- Visits every entrance mark within its strength: every minimap entrance row
  on a non-level-interior map, except a cell the strength bracket closed
  under ``over_strength_ceiling`` for this loadout, must be looked.
- Records what each needs: ``looked`` with a ``block_type``; a locked mark
  also has ``needs: key`` (Manual §9.2, §11).
- Returns to town: the character stands on the town cell from the knowledge
  base when the run ends. ``on_entrances_done`` fires once when the catalog
  is complete; the smoke script uses it to put ``travel:town`` in the
  profile's directives, so Travel (A27) walks the agent home.

Survival gates match M7 (``acceptance_survival.py``): death, retreat miss,
unsafe Recover, loops and API errors fail immediately. Heal actions are
reported, not gated.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
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
    return _unrecorded(kb, required_entrance_marks(kb, bracket))


def _unrecorded(kb: KnowledgeBase, required: set[tuple[int, Pos]]) -> list[tuple[int, Pos]]:
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
    alive (whichever comes first). ``at_town_end`` is the last decision's
    position, so leaving town after a visit clears it.
    """

    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    on_entrances_done: Callable[[], None] | None = None
    marks_known: int = 0
    catalog_size: int = 0
    entrances_recorded: int = 0
    strength_closed_skipped: int = 0
    at_town_end: bool = False
    _entrances_done_sent: bool = False

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
        if self.entrances_ok() and not self._entrances_done_sent:
            self._entrances_done_sent = True
            if self.on_entrances_done is not None:
                self.on_entrances_done()
        town = town_from_kb(knowledge)
        self.at_town_end = town is not None and w.map_id == town[0] and w.pos == town[1]
        if self.at_town_end and self.entrances_ok() and self.stop is not None:
            self.stop.set()

    def _refresh_entrances(self, kb: KnowledgeBase | None, bracket: StrengthBracket) -> None:
        if kb is None:
            return
        all_marks = {(mid, pos) for mid, pos, _ in iter_entrances(kb) if not is_level_interior(kb, mid)}
        if not all_marks:
            return
        required = required_entrance_marks(kb, bracket)
        self.marks_known = len(all_marks)
        self.strength_closed_skipped = len(all_marks - required)
        self.catalog_size = len(required)
        self.entrances_recorded = len(required) - len(_unrecorded(kb, required))

    def entrances_ok(self) -> bool:
        """Every required mark recorded. Vacuously true when the minimap listed
        marks but the strength bracket closed all of them."""
        return self.marks_known > 0 and self.entrances_recorded >= self.catalog_size

    def failures(self, *, full_run: bool = True) -> list[str]:
        out = list(self.survival_failures())
        if full_run:
            if not self.marks_known:
                out.append("no entrance catalog from minimap")
            elif not self.entrances_ok():
                missing = self.catalog_size - self.entrances_recorded
                out.append(f"{missing} entrance mark(s) not looked and recorded")
            if not self.at_town_end:
                out.append("character not on town at end")
        return out

    def summary_lines(self) -> list[str]:
        lines = [
            f"entrance marks known: {self.marks_known}",
            f"entrance marks required: {self.catalog_size} (skipped as over-strength: {self.strength_closed_skipped})",
            f"entrance marks recorded: {self.entrances_recorded}",
            f"on town at end: {self.at_town_end}",
        ]
        lines.extend(self.survival_summary_lines())
        return lines
