"""Shared pieces of timed live acceptance runs (M7 A16, M8 A25, M9 A29, M10 A33, M11 A40)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance import CountingClient, PlannerHealth

# A run at least this share of its target duration is judged on the full gate.
FULL_RUN_FRACTION = 0.95


@dataclass(kw_only=True)
class TimedRunHooks(PlannerHealth):
    """Deaths, API errors, planner health (``PlannerHealth``), and an optional wall-clock stop for long smoke runs."""

    stop: threading.Event | None = None
    target_seconds: float | None = None  # wall-clock length of the run; None plays until stopped
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    # Off (the default): deaths are counted and reported, and play goes on
    # after the respawn, since a run that takes risks will die sometimes. On:
    # the no-death gate, where the first death fails the run and ends it.
    stop_on_death: bool = False
    deaths: int = 0
    api_errors: list[str] = field(default_factory=list)

    def wrap(self, client):
        return CountingClient(client, self.api_errors)

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        if (
            self.stop is not None
            and self.target_seconds is not None
            and alive
            and now - self.started_at >= self.target_seconds
        ):
            self.stop.set()

    def on_death(self) -> None:
        """Count it. Under ``stop_on_death`` it already fails the run, so end it rather than play on."""
        self.deaths += 1
        if self.stop_on_death and self.stop is not None:
            self.stop.set()

    def base_failures(self) -> list[str]:
        out: list[str] = []
        if self.deaths and self.stop_on_death:
            out.append(f"{self.deaths} death(s) during run")
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        out.extend(self.planner_failures())
        return out
