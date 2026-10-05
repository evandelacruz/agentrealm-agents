"""M10 acceptance metrics (A33): curiosity along the route and odd-block discipline.

The M10 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks:

- Readable cells: every cell that was ``readable`` and in sight along the route
  has an applied ``Read`` recorded in the knowledge base (``read_cells``).
- NPCs: every NPC that came within 25 blocks (Chebyshev) has an applied ``Say``
  recorded (``spoken_npcs``).
- Odd block: on the navigation fixture map ``ODD_BUSH`` (``grids.py``), the
  agent finds and opens the lone bush; CI runs that offline, not the live smoke.
- Break memory: the agent never sends a Break ``Use`` on a (block, capability)
  pair the knowledge base already holds as failed. An opened pair may be
  broken again once the block regrows.

Live smoke runs explore for ``TARGET_SECONDS``; unread signs and unspoken NPCs
are gated only on a run of at least 95% of that target, like M7 navigation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance import AcceptanceHooks, CountingClient
from .break_memory import attempt_failed, attempt_open
from .config import Policy
from .interest_list import SPEECH_RANGE, cell_was_read, in_sight, spoken_npc_ids
from .item_table import use_target_block
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .plan import CAPABILITIES
from .world import Pos, WorldModel, chebyshev

TARGET_SECONDS = 3600.0
FULL_RUN_FRACTION = 0.95


@dataclass
class M10AcceptanceMetrics(AcceptanceHooks):
    """Counts curiosity coverage and break discipline while the runner plays.

    ``stop`` is set once ``target_seconds`` have passed by ``clock`` with the
    character alive.
    """

    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    deaths: int = 0
    api_errors: list[str] = field(default_factory=list)
    duplicate_break_attempts: int = 0
    _seen_readable: set[tuple[int, Pos]] = field(default_factory=set)
    _seen_npcs: set[int] = field(default_factory=set)

    def wrap(self, client):
        return CountingClient(client, self.api_errors)

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        if self.stop is not None and alive and now - self.started_at >= self.target_seconds:
            self.stop.set()

    def on_death(self) -> None:
        self.deaths += 1
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
        self._note_sight(w, knowledge)
        self._note_duplicate_break(w, m, intents, knowledge)

    def _note_sight(self, w: WorldModel, knowledge: KnowledgeBase | None) -> None:
        if w.pos is None or w.map_id is None:
            return
        here, map_id = w.pos, w.map_id
        for pos, flag in w.view.readable.items():
            if flag and in_sight(w, map_id, here, pos):
                self._seen_readable.add((map_id, pos))
        for ent in w.entities:
            if ent.kind == "npc" and chebyshev(here, ent.pos) <= SPEECH_RANGE:
                self._seen_npcs.add(ent.id)

    def _note_duplicate_break(
        self, w: WorldModel, m: Memory, intents: list[dict] | None, knowledge: KnowledgeBase | None
    ) -> None:
        """Count a Break ``Use`` sent on a pair already failed; a held queue sends nothing."""
        pending = m.break_pending
        if pending is None or not intents:
            return
        map_id, pos, cap = pending
        sends_use = any(i.get("verb") == "Use" and use_target_block(i, w.entities) == pos for i in intents)
        if sends_use and attempt_failed(knowledge, map_id, pos, cap):
            self.duplicate_break_attempts += 1

    def missed_reads(self, knowledge: KnowledgeBase | None) -> list[tuple[int, Pos]]:
        return sorted(
            (map_id, pos)
            for map_id, pos in self._seen_readable
            if not cell_was_read(knowledge, map_id, pos)
        )

    def missed_npcs(self, knowledge: KnowledgeBase | None) -> list[int]:
        spoken = spoken_npc_ids(knowledge)
        return sorted(npc_id for npc_id in self._seen_npcs if npc_id not in spoken)

    def failures(self, *, full_run: bool = True, knowledge: KnowledgeBase | None = None) -> list[str]:
        out: list[str] = []
        if self.deaths:
            out.append(f"{self.deaths} death(s) during run")
        if self.duplicate_break_attempts:
            out.append(
                f"{self.duplicate_break_attempts} Break attempt(s) on a (block, capability) already failed"
            )
        if full_run:
            missed = self.missed_reads(knowledge)
            if missed:
                out.append(f"{len(missed)} readable cell(s) in sight never read (e.g. {missed[0]})")
            missed_npc = self.missed_npcs(knowledge)
            if missed_npc:
                out.append(f"{len(missed_npc)} NPC(s) within 25 blocks never spoken to (e.g. {missed_npc[0]})")
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self, knowledge: KnowledgeBase | None = None) -> list[str]:
        missed_r = self.missed_reads(knowledge)
        missed_n = self.missed_npcs(knowledge)
        return [
            f"readable cells seen in sight: {len(self._seen_readable)} (unread: {len(missed_r)})",
            f"NPCs within {SPEECH_RANGE} blocks seen: {len(self._seen_npcs)} (never spoken: {len(missed_n)})",
            f"duplicate break attempts: {self.duplicate_break_attempts}",
            f"deaths: {self.deaths}",
            f"API errors: {len(self.api_errors)}",
        ]


def odd_block_opened(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    """True when a break opened the odd block at ``pos`` on ``map_id`` (fixture gate)."""
    for cap in sorted(CAPABILITIES):
        if attempt_open(kb, map_id, pos, cap):
            return True
    return False
