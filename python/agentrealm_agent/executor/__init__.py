"""Paced multi-intent queues for real-time play (M6).

Types, limits and paced Step/Wait queue building; the runner wires them in later.
"""

from .constants import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
)
from .intents import IntentQueue, StepDirection, StepIntent, WaitIntent, step, wait
from .movement import build_paced_walk_queue, direction_between, ticks_per_step
from .queue import trim_to_horizon, within_horizon

__all__ = [
    "DEFAULT_QUEUE_HORIZON_SECONDS",
    "DEFAULT_TICK_RATE_HZ",
    "IntentQueue",
    "QUEUE_HORIZON_INTENTS",
    "StepDirection",
    "StepIntent",
    "WaitIntent",
    "build_paced_walk_queue",
    "direction_between",
    "queue_horizon_intents",
    "step",
    "ticks_per_step",
    "trim_to_horizon",
    "wait",
    "within_horizon",
]
