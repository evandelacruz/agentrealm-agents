"""The executive: carries out what the strategist published (docs/v3/SPEC.md **The executive and the methods**).

The strategist publishes a goal, an approach to it, a plan B and the goal
after it. Every decision the executive checks the goal's test and the
running approach's failure conditions:

- the goal met: report done, and start ``next`` at once;
- the approach failed: report why, and switch to plan B at once;
- plan B failed too, or nothing is published: run the agent's fallback
  (safe, useful work) until the strategist answers.

It never chooses a goal or an approach itself, and never waits for the
strategist: there is always something to run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Goal(Protocol):
    text: str

    def met(self, ctx: Any) -> bool: ...


class Approach(Protocol):
    text: str

    def skill(self, ctx: Any) -> Any:
        """The method's skill, made when the approach starts."""

    def failed(self, ctx: Any, skill: Any, seconds: float, stalled: float) -> str | None:
        """Why the approach cannot go on, or None. ``seconds`` it has run; ``stalled`` seconds with no progress."""


@dataclass
class Publication:
    goal: Goal
    why: str
    approach: Approach
    plan_b: Approach | None = None
    next: Publication | None = None


@dataclass
class Report:
    """What the executive tells the strategist: a goal met, an approach failed, a fallback started."""

    goal: str
    approach: str
    result: str  # done | failed | fallback
    why: str = ""
    at: float = 0.0

    def __str__(self) -> str:
        return f"{self.result}: {self.goal} by {self.approach}" + (f" ({self.why})" if self.why else "")


class Executive:
    def __init__(self, fallback: Callable[[Any], tuple[Goal, Approach]]):
        self.fallback = fallback
        self.publication: Publication | None = None
        self.goal: Goal | None = None
        self.approach: Approach | None = None
        self.plan = ""  # A, B or fallback
        self.skill: Any = None
        self.reports: list[Report] = []
        self._started = 0.0
        self._best = float("-inf")
        self._best_at = 0.0

    def adopt(self, publication: Publication, ctx: Any, now: float | None = None) -> None:
        """A new publication replaces what is running."""
        self.publication = publication
        self._start(publication.goal, publication.approach, "A", ctx, now)

    def step(self, ctx: Any, now: float | None = None) -> Any:
        """The skill to run this decision."""
        now = time.time() if now is None else now
        if self.goal is None:
            self._fall_back(ctx, now, "nothing published yet")
        elif self.plan != "fallback" and self.goal.met(ctx):
            self._report("done", "", now)
            nxt = self.publication.next if self.publication else None
            if nxt is not None:
                self.publication = nxt
                self._start(nxt.goal, nxt.approach, "A", ctx, now)
            else:
                self.publication = None
                self._fall_back(ctx, now, "the goal is met and nothing comes next")
        else:
            why = self.approach.failed(ctx, self.skill, now - self._started, self._stalled(ctx, now))
            if why is not None:
                self._report("failed", why, now)
                plan_b = self.publication.plan_b if self.publication else None
                if self.plan == "A" and plan_b is not None:
                    self._start(self.goal, plan_b, "B", ctx, now)
                elif self.plan != "fallback":
                    self._fall_back(ctx, now, "plan B failed too" if self.plan == "B" else "no plan B")
        return self.skill

    def _stalled(self, ctx: Any, now: float) -> float:
        p = self.skill.progress(ctx) if self.skill is not None else 0.0
        if p > self._best:
            self._best, self._best_at = p, now
        return now - self._best_at

    def _start(self, goal: Goal, approach: Approach, plan: str, ctx: Any, now: float | None) -> None:
        now = time.time() if now is None else now
        self.goal, self.approach, self.plan = goal, approach, plan
        self.skill = approach.skill(ctx)
        self._started, self._best, self._best_at = now, float("-inf"), now

    def _fall_back(self, ctx: Any, now: float, why: str) -> None:
        goal, approach = self.fallback(ctx)
        self._start(goal, approach, "fallback", ctx, now)
        self._report("fallback", why, now)

    def _report(self, result: str, why: str, now: float) -> None:
        self.reports = (self.reports + [Report(self.goal.text, self.approach.text, result, why, now)])[-20:]

