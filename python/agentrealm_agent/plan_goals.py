"""Goal-stack ops the state machine consumes before the strategist (A22, A34)."""

from __future__ import annotations

from dataclasses import dataclass

from .directives import Directives


@dataclass(frozen=True)
class GatherGemsGoal:
    count: int


def gather_gems_goal(directives: Directives) -> GatherGemsGoal | None:
    """The first ``gather_gems[:count]`` op on the directives goal stack."""
    for raw in directives.goals:
        if raw == "gather_gems":
            return GatherGemsGoal(1)
        if raw.startswith("gather_gems:"):
            try:
                n = int(raw.split(":", 1)[1])
            except ValueError:
                continue
            if n > 0:
                return GatherGemsGoal(n)
    return None
