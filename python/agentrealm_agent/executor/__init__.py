"""Paced multi-intent queues for real-time play (M6).

Types, limits and paced Step/Wait queue building (``movement``), used by the
runner, and attack and speech pacing (``pacing``), used for ``Use`` and
``Say``/``Broadcast`` in the runner.
"""

from .constants import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
)
from .intents import IntentQueue, StepDirection, StepIntent, WaitIntent, step, wait
from .movement import build_paced_walk_queue, direction_between, step_landing, ticks_per_step
from .pacing import (
    DEFAULT_WEAPON_COOLDOWN_TICKS,
    SPEECH_INTERVAL_TICKS,
    arm_then_use,
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
    "arm_then_use",
    "build_attack_queue",
    "build_paced_walk_queue",
    "direction_between",
    "pace_speech",
    "pace_uses",
    "queue_horizon_intents",
    "step",
    "step_landing",
    "ticks_per_step",
    "trim_to_horizon",
    "uses_attack_cooldown",
    "uses_speech_cooldown",
    "wait",
    "waits",
    "within_horizon",
]
