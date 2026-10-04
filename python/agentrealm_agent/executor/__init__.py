"""Paced multi-intent queues for real-time play (M6).

The runner and state machine will build queues here. So far: types, limits, and
attack and speech pacing (``pacing``); the runner does not use them yet.
"""

from .constants import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
)
from .intents import IntentQueue, StepDirection, StepIntent, WaitIntent, step, wait
from .pacing import (
    DEFAULT_WEAPON_COOLDOWN_TICKS,
    SPEECH_INTERVAL_TICKS,
    build_attack_queue,
    pace_speech,
    pace_uses,
    uses_attack_cooldown,
    uses_speech_cooldown,
    waits,
)
from .queue import trim_to_horizon, within_horizon

__all__ = [
    "DEFAULT_QUEUE_HORIZON_SECONDS",
    "DEFAULT_TICK_RATE_HZ",
    "DEFAULT_WEAPON_COOLDOWN_TICKS",
    "IntentQueue",
    "QUEUE_HORIZON_INTENTS",
    "SPEECH_INTERVAL_TICKS",
    "StepDirection",
    "StepIntent",
    "WaitIntent",
    "build_attack_queue",
    "pace_speech",
    "pace_uses",
    "queue_horizon_intents",
    "step",
    "trim_to_horizon",
    "uses_attack_cooldown",
    "uses_speech_cooldown",
    "wait",
    "waits",
    "within_horizon",
]
