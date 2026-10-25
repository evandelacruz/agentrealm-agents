"""The controller: scores every action open this decision and takes the best (docs/v3/SPEC.md **Controller**).

An action's score is the sum of its considerations' scores, each the change
the action makes to one thing the agent values (task progress, health,
danger, gems), times that consideration's weight. Survival wins when it
matters because its scores are large then, not because it is checked first.
"""

from __future__ import annotations

from dataclasses import dataclass

from .interfaces import Action, Consideration, Context, State


@dataclass
class Scored:
    action: Action
    total: float
    parts: dict[str, float]

    def brief(self) -> dict:
        return {"action": str(self.action), "why": self.action.why, "score": round(self.total, 3),
                "parts": {k: round(v, 3) for k, v in self.parts.items() if v}}


class Controller:
    def __init__(self, considerations: list[Consideration], weights: dict[str, float] | None = None):
        self.considerations = considerations
        self.weights = weights or {}

    def score(self, a: Action, s: State, ctx: Context) -> Scored:
        parts = {c.name: self.weights.get(c.name, 1.0) * c.score(a, s, ctx) for c in self.considerations}
        return Scored(a, sum(parts.values()), parts)

    def rank(self, actions: list[Action], s: State, ctx: Context) -> list[Scored]:
        """Every action scored, best first; ties keep the order they were offered in."""
        scored = [self.score(a, s, ctx) for a in actions]
        return sorted(scored, key=lambda x: -x.total)
