"""Pure helpers for intent queues before the runner sends them."""

from __future__ import annotations

from .constants import QUEUE_HORIZON_INTENTS, queue_horizon_intents
from .intents import Intent, IntentQueue


def trim_to_horizon(
    queue: IntentQueue,
    *,
    limit: int = QUEUE_HORIZON_INTENTS,
) -> IntentQueue:
    """Keep the prefix that fits the world's queue horizon; drop the rest."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return list(queue[:limit])


def within_horizon(
    queue: IntentQueue,
    *,
    limit: int = QUEUE_HORIZON_INTENTS,
) -> bool:
    """True when posting the whole queue would not hit queue_too_long."""
    return len(queue) <= limit


def horizon_for_world(
    *,
    tick_rate_hz: int,
    horizon_seconds: int,
) -> int:
    """Intent cap for a world read from GET .../world (queue_horizon_seconds × tick rate)."""
    return queue_horizon_intents(
        tick_rate_hz=tick_rate_hz,
        horizon_seconds=horizon_seconds,
    )
