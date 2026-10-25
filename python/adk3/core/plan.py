"""A plan: the strategist's goal and the ordered tasks that reach it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Step:
    """One task in a plan: its kind, its arguments, and why it is there."""

    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def __str__(self) -> str:
        args = " ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.kind}({args})" if args else self.kind


@dataclass(frozen=True)
class Plan:
    """The strategist's answer. ``risk`` (0 to 1) is how much danger the controller may accept for progress."""

    goal: str
    steps: tuple[Step, ...]
    why: str = ""
    risk: float = 0.5


class PlanError(ValueError):
    pass


def parse_plan(raw: dict[str, Any], kinds: set[str]) -> Plan:
    """A plan from the strategist's JSON. Unknown task kinds are refused, so the strategist hears why."""
    if not isinstance(raw, dict):
        raise PlanError("the reply is not a JSON object")
    steps = []
    for i, item in enumerate(raw.get("plan") or []):
        if not isinstance(item, dict) or not isinstance(item.get("task"), str):
            raise PlanError(f"plan[{i}] has no task")
        kind = item["task"]
        if kind not in kinds:
            raise PlanError(f"plan[{i}]: unknown task {kind!r}; known: {', '.join(sorted(kinds))}")
        args = {k: v for k, v in item.items() if k not in ("task", "why")}
        steps.append(Step(kind, args, str(item.get("why", ""))))
    if not steps:
        raise PlanError("the plan has no tasks")
    risk = raw.get("risk", 0.5)
    risk = min(1.0, max(0.0, float(risk))) if isinstance(risk, (int, float)) else 0.5
    return Plan(str(raw.get("goal", "")), tuple(steps), str(raw.get("why", "")), risk)
