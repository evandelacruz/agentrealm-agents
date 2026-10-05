"""M4 acceptance metrics (A36): strategist plans clue-driven ops and states run them.

The M4 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks:

- Given clues from a test world, the strategist replaces the goal stack with
  the right ``buy``, ``travel`` and ``break_block`` operations (PLAYABLE_AGENT_PLAN
  Strategist example, invented coordinates).
- The state machine carries each operation out: ``Shop`` for ``buy``, ``Travel``
  for ``travel``, ``Break`` for ``break_block`` while that op is on top of the
  stack and the round sends a new queue (``intents`` is not None).

Clue triggers and API faults are reported. A live run on the public API also
needs the strategist enabled and at least one clue trigger drained to the
model; the offline fixture in ``tests/test_m4_acceptance.py`` drives pass and
fail without keys.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance import AcceptanceHooks, CountingClient
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .plan import OP_STATE
from .world import WorldModel

TARGET_SECONDS = 3600.0

# Invented test-world ops from docs/PLAYABLE_AGENT_PLAN.md (Strategist example).
GATE_OPS: tuple[dict, ...] = (
    {"op": "buy", "code": "torch"},
    {"op": "travel", "to": "entrance", "x": 120, "y": 40},
    {"op": "break_block", "x": 118, "y": 41, "capability": "burn"},
)

GATE_OP_NAMES = frozenset({"buy", "travel", "break_block"})


def op_key(op: dict) -> str:
    """Stable key for matching a plan op to a gate requirement."""
    name = op["op"]
    if name == "buy":
        return f"buy:{op['code']}"
    if name == "travel":
        mid = op.get("map_id")
        extra = f",m{mid}" if mid is not None else ""
        return f"travel:{op['to']}:{op['x']},{op['y']}{extra}"
    if name == "break_block":
        return f"break_block:{op['x']},{op['y']}:{op['capability']}"
    return name


def op_matches(required: dict, candidate: dict) -> bool:
    """True when ``candidate`` has every field ``required`` names."""
    if candidate.get("op") != required.get("op"):
        return False
    for key, value in required.items():
        if key == "op":
            continue
        if candidate.get(key) != value:
            return False
    return True


def gate_key(required: dict) -> str:
    return op_key(required)


@dataclass
class M4AcceptanceMetrics(AcceptanceHooks):
    """Counts strategist planning and state execution for the M4 gate ops."""

    required: tuple[dict, ...] = GATE_OPS
    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    clue_triggers: int = 0
    strategist_applied: int = 0
    planned: set[str] = field(default_factory=set)
    executed: set[str] = field(default_factory=set)
    wrong_state: list[str] = field(default_factory=list)
    api_errors: list[str] = field(default_factory=list)

    def wrap(self, client):
        return CountingClient(client, self.api_errors)

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        if self.stop is not None and alive and now - self.started_at >= self.target_seconds:
            self.stop.set()

    def on_strategist_applied(self, goals: list[dict]) -> None:
        self.strategist_applied += 1
        for goal in goals:
            if goal.get("op") not in GATE_OP_NAMES:
                continue
            for req in self.required:
                if op_matches(req, goal):
                    self.planned.add(gate_key(req))

    def note_clue_trigger(self) -> None:
        """One clue signal reached the strategist inbox (tests or trace replay)."""
        self.clue_triggers += 1

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
        plan_op: dict | None = None,
    ) -> None:
        if plan_op is None or intents is None:
            return
        op_name = plan_op.get("op")
        if op_name not in GATE_OP_NAMES:
            return
        owner = OP_STATE.get(op_name)
        if owner is None:
            return
        for req in self.required:
            if not op_matches(req, plan_op):
                continue
            key = gate_key(req)
            if state == owner:
                self.executed.add(key)
            else:
                self.wrong_state.append(f"{key} saw state {state}, expected {owner}")

    def failures(self, *, full: bool = True) -> list[str]:
        out: list[str] = []
        if full:
            for req in self.required:
                key = gate_key(req)
                if key not in self.planned:
                    out.append(f"strategist never planned {key}")
                if key not in self.executed:
                    out.append(f"state machine never executed {key}")
        if full and self.clue_triggers < 1:
            out.append("no clue trigger reached the strategist")
        if self.wrong_state:
            out.append(f"wrong state for gate op(s): {'; '.join(self.wrong_state[:3])}")
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self) -> list[str]:
        keys = [gate_key(r) for r in self.required]
        return [
            f"clue triggers: {self.clue_triggers}",
            f"strategist applied: {self.strategist_applied}",
            f"planned ({len(self.planned)}/{len(self.required)}): {', '.join(sorted(self.planned)) or 'none'}",
            f"executed ({len(self.executed)}/{len(self.required)}): {', '.join(sorted(self.executed)) or 'none'}",
            f"missing planned: {', '.join(k for k in keys if k not in self.planned) or 'none'}",
            f"missing executed: {', '.join(k for k in keys if k not in self.executed) or 'none'}",
            f"wrong state: {len(self.wrong_state)}",
            f"API errors: {len(self.api_errors)}",
        ]
