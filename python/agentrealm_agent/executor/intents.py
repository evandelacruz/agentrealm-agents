"""Wire-shaped intents for paced multi-intent queues (M6).

Shapes follow the `tick` intent schema (docs/GAME_NOTES.md, Step). Each verb gets
a TypedDict; at runtime they are plain dicts, so they go straight to `Client.tick`.
"""

from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict

from ..client import Intent

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


class StepIntent(TypedDict):
    verb: Literal["Step"]
    direction: StepDirection


class WaitIntent(TypedDict):
    verb: Literal["Wait"]


# An ordered queue for one POST tick; mixes these with the verbs other states build.
IntentQueue: TypeAlias = list[Intent]


def step(direction: StepDirection) -> StepIntent:
    """Move one block in direction from wherever the character stands when it runs."""
    return {"verb": "Step", "direction": direction}


def wait() -> WaitIntent:
    """Idle one tick inside a queue; counts as activity for auto-sleep (B45)."""
    return {"verb": "Wait"}
