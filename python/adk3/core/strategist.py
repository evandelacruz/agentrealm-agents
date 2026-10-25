"""The strategist off the decision thread, and when to ask it (docs/v3/SPEC.md **Replan triggers**).

``Asker`` runs one strategist call at a time on its own thread; the loop
keeps the old plan until the reply lands, so a slow call never costs a
decision. ``Triggers`` says when a new plan is worth asking for.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .interfaces import Report, State, Strategist
from .plan import Plan


class Asker:
    def __init__(self, strategist: Strategist):
        self.strategist = strategist
        self._thread: threading.Thread | None = None
        self._reply: Plan | None = None
        self._error = ""
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ask(self, s: State, report: Report) -> bool:
        if self.busy():
            return False

        def call() -> None:
            try:
                plan, error = self.strategist.plan(s, report), ""
            except Exception as e:  # a failed call must never stop play
                plan, error = None, f"{type(e).__name__}: {e}"
            with self._lock:
                self._reply, self._error = plan, error

        self._thread = threading.Thread(target=call, daemon=True)
        self._thread.start()
        return True

    def take(self) -> tuple[Plan | None, str]:
        """The reply that landed since the last take, if any, and the call's error."""
        with self._lock:
            reply, error, self._reply, self._error = self._reply, self._error, None, ""
        return reply, error


@dataclass
class Triggers:
    """When to ask: the plan is over, a task failed, a surprise, or the plan has grown old.

    Never more often than ``min_seconds``, so a burst of events is one ask.
    """

    min_seconds: float = 10.0
    max_age_seconds: float = 120.0
    last_ask: float = float("-inf")

    def reason(self, now: float, *, no_plan: bool, plan_over: bool, events: list[str]) -> str | None:
        if now - self.last_ask < self.min_seconds:
            return None
        if no_plan:
            return "no plan yet"
        if plan_over:
            return "the plan is finished"
        if events:
            return "; ".join(events)
        if now - self.last_ask >= self.max_age_seconds:
            return f"the plan is {self.max_age_seconds:.0f}s old"
        return None

    def asked(self, now: float | None = None) -> None:
        self.last_ask = time.time() if now is None else now
