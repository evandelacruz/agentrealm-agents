"""Paced multi-intent queues for real-time play (M6).

Types and limits, paced Step/Wait queue building (movement.py), and the
runner's queue invalidation (invalidation.py).
"""

from .constants import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
)
from .intents import IntentQueue, StepDirection, StepIntent, WaitIntent, step, wait
from .invalidation import Executor, InFlight, paced_set_positions, set_position
from .movement import build_paced_walk_queue, direction_between, ticks_per_step
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
    "build_paced_walk_queue",
    "direction_between",
    "paced_set_positions",
    "queue_horizon_intents",
    "set_position",
    "step",
    "ticks_per_step",
    "trim_to_horizon",
    "wait",
    "within_horizon",
]
