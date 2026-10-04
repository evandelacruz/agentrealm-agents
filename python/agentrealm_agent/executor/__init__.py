"""Paced multi-intent queues for real-time play (M6).

Types and limits, plus the runner's queue invalidation (invalidation.py).
"""

from .constants import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
)
from .intents import IntentQueue, StepDirection, StepIntent, WaitIntent, step, wait
from .invalidation import Executor, InFlight, paced_set_positions, set_position, ticks_per_step
from .queue import trim_to_horizon, within_horizon

__all__ = [
    "DEFAULT_QUEUE_HORIZON_SECONDS",
    "DEFAULT_TICK_RATE_HZ",
    "Executor",
    "InFlight",
    "IntentQueue",
    "QUEUE_HORIZON_INTENTS",
    "StepDirection",
    "StepIntent",
    "WaitIntent",
    "paced_set_positions",
    "queue_horizon_intents",
    "set_position",
    "step",
    "ticks_per_step",
    "trim_to_horizon",
    "wait",
    "within_horizon",
]
