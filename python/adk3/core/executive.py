"""The executive: holds the plan and runs its tasks in order (docs/v3/SPEC.md **Executive**).

Each decision it checks the head task: done moves on, failed moves on with
why, and a task whose progress has not risen for ``stall_seconds`` fails as
stalled. Every task over is an ``Outcome`` the strategist is told. It never
improvises: with the plan run out it has no task until the next plan lands.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .interfaces import Outcome, State, Task
from .plan import Plan, Step

OUTCOMES_KEPT = 12


class Executive:
    def __init__(self, build: Callable[[Step], Task], stall_seconds: float = 45.0):
        self.build = build
        self.stall_seconds = stall_seconds
        self.plan: Plan | None = None
        self.index = 0
        self.task: Task | None = None
        self.outcomes: list[Outcome] = []
        self.new_outcomes = 0  # outcomes since the strategist was last asked
        self._best = float("-inf")
        self._best_at = 0.0

    def adopt(self, plan: Plan, now: float | None = None) -> None:
        """Take a new plan in place of the old; the task under way is dropped unless the new plan starts with it."""
        keep = self.task is not None and same(plan.steps[0], self.task.step)
        if self.task is not None and not keep:
            self._over("dropped", "a new plan")
        self.plan, self.index = plan, 0
        if not keep:
            self._start(now)

    def step(self, s: State, now: float | None = None) -> Task | None:
        """The task to work this decision, after moving past the ones over."""
        now = time.time() if now is None else now
        while self.task is not None:
            task = self.task
            if task.done(s):
                self._over("done")
            elif (why := task.failed(s)) is not None:
                self._over("failed", why)
            else:
                p = task.progress(s)
                if p > self._best:
                    self._best, self._best_at = p, now
                    return task
                if now - self._best_at < self.stall_seconds:
                    return task
                self._over("failed", f"stalled: no progress for {self.stall_seconds:.0f}s")
            self.index += 1
            self._start(now)
        return None

    def finished(self) -> bool:
        return self.task is None

    def take_outcomes(self) -> int:
        n, self.new_outcomes = self.new_outcomes, 0
        return n

    def _start(self, now: float | None) -> None:
        steps = self.plan.steps if self.plan is not None else ()
        self.task = self.build(steps[self.index]) if self.index < len(steps) else None
        self._best, self._best_at = float("-inf"), time.time() if now is None else now

    def _over(self, result: str, why: str = "") -> None:
        assert self.task is not None
        self.outcomes = (self.outcomes + [Outcome(self.task.step, result, why)])[-OUTCOMES_KEPT:]
        self.new_outcomes += 1
        self.task = None


def same(a: Step, b: Step) -> bool:
    """The same task: kind and arguments; the why may be worded anew."""
    return a.kind == b.kind and a.args == b.args
