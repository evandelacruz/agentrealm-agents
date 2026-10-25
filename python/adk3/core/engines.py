"""Real-time play: proposers and one judge (docs/v3/SPEC.md **Engines**).

Every tick each proposer offers its options for the moment: the skill's
ways to do its work, combat's fight or back off, the opportunities in
reach. One judge scores every option with one function and takes the best.
Two parts of the agent can therefore never disagree about the same move:
there is one calculation, and it answers once.

The judge commits: the option under way (by its key: a destination, a
target, a pickup) is kept unless another beats it by ``margin``, so a
choice is never flipped by small changes in the moment.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Proposer(Protocol):
    name: str

    def propose(self, ctx: Any, order: Any) -> list[Any]:
        """The options this proposer sees now; each has a ``key`` naming what it commits to."""


@dataclass
class Scored:
    option: Any
    score: float
    by: str  # the proposer


class Judge:
    def __init__(self, score: Callable[[Any, Any], float], margin: float):
        self.score, self.margin = score, margin
        self.committed: Any = None  # the key of the option under way

    def choose(self, ctx: Any, proposers: list[Proposer], order: Any) -> Scored | None:
        scored = [Scored(o, self.score(ctx, o), p.name) for p in proposers for o in p.propose(ctx, order)]
        if not scored:
            self.committed = None
            return None
        best = max(scored, key=lambda x: x.score)
        kept = next((x for x in scored if x.option.key == self.committed), None)
        if kept is not None and kept.score >= best.score - self.margin:
            best = kept
        self.committed = best.option.key
        return best
