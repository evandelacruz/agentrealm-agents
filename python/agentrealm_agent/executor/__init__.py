"""Paced multi-intent queues for real-time play (M6).

The runner and state machine will build queues here; this slice is types and limits only.
"""

from .constants import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
)
from .intents import Intent, IntentQueue, StepDirection, step, wait
from .queue import horizon_for_world, trim_to_horizon, within_horizon

__all__ = [
    "DEFAULT_QUEUE_HORIZON_SECONDS",
    "DEFAULT_TICK_RATE_HZ",
    "Intent",
    "IntentQueue",
    "QUEUE_HORIZON_INTENTS",
    "StepDirection",
    "horizon_for_world",
    "queue_horizon_intents",
    "step",
    "trim_to_horizon",
    "wait",
    "within_horizon",
]
