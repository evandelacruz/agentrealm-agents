"""Wire-shaped intents for paced multi-intent queues (M6)."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

StepDirection = Literal[
    "up",
    "down",
    "left",
    "right",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
]

# POST /characters/{id}/tick accepts a JSON object per intent; other verbs join later.
Intent: TypeAlias = dict[str, Any]
IntentQueue: TypeAlias = list[Intent]


def step(direction: StepDirection) -> Intent:
    """Move one block in direction when this intent runs (manual Intent reference, B101)."""
    return {"verb": "Step", "direction": direction}


def wait() -> Intent:
    """Idle one tick inside a queue; counts as activity for auto-sleep (B45)."""
    return {"verb": "Wait"}
