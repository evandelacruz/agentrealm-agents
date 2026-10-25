"""Skills and the executive (docs/v3/SPEC.md **Executive and skills**).

A skill is a procedure that satisfies one kind of need: travel somewhere,
earn gems, buy an item. Each decision it says what it wants now, its
**order** (go to this place, work these blocks, talk to this helper), which
the engines carry out in real time. It also says when it is done, when it
cannot go on and why, and a progress number that rises as it nears done.

The executive runs the skill of the focus need. A skill that fails, or
whose progress has not risen for ``stall_seconds``, blocks its need with
why, and the graph walks on to the next thing to work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from .needs import Graph, Need


class Skill(Protocol):
    name: str

    def order(self, ctx: Any) -> Any:
        """What the skill wants of the engines now, or None when it has nothing to ask."""

    def done(self, ctx: Any) -> bool: ...

    def failed(self, ctx: Any) -> str | None:
        """Why the skill cannot go on, or None."""

    def progress(self, ctx: Any) -> float: ...


@dataclass
class Outcome:
    """A skill over, for the strategist and the run report."""

    need: str
    skill: str
    result: str  # done | failed
    why: str = ""
    at: float = 0.0


class Executive:
    def __init__(self, graph: Graph, stall_seconds: float = 60.0):
        self.graph = graph
        self.stall_seconds = stall_seconds
        self.need: Need | None = None
        self.skill: Skill | None = None
        self.outcomes: list[Outcome] = []
        self._best = float("-inf")
        self._best_at = 0.0

    def step(self, ctx: Any, path: list[Need], revision: int, now: float | None = None) -> Skill | None:
        """The skill to run this decision, for the need at the end of ``path``."""
        now = time.time() if now is None else now
        leaf = path[-1] if path else None
        if self.skill is not None and self.need is not None:
            if self.skill.done(ctx):
                self._over("done", "", now)
            elif (why := self.skill.failed(ctx)) is not None:
                self._fail(why, revision, now)
            elif self._stalled(ctx, now):
                self._fail(f"no progress for {self.stall_seconds:.0f}s", revision, now)
        if leaf is None:
            self.need, self.skill = None, None
            return None
        if self.need is None or leaf.key != self.need.key:
            self.need, self.skill = leaf, leaf.skill(ctx)
            self._best, self._best_at = float("-inf"), now
        return self.skill

    def _stalled(self, ctx: Any, now: float) -> bool:
        p = self.skill.progress(ctx)
        if p > self._best:
            self._best, self._best_at = p, now
            return False
        return now - self._best_at >= self.stall_seconds

    def _fail(self, why: str, revision: int, now: float) -> None:
        self.graph.block(self.need, why, revision)
        self._over("failed", why, now)

    def _over(self, result: str, why: str, now: float) -> None:
        self.outcomes = (self.outcomes + [Outcome(self.need.key, self.skill.name, result, why, now)])[-20:]
        self.need, self.skill = None, None
