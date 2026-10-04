"""Limits shared with the Agent Realm intent queue (manual §7.6, GAME_NOTES)."""

from __future__ import annotations

DEFAULT_TICK_RATE_HZ = 10
DEFAULT_QUEUE_HORIZON_SECONDS = 4

# worlds.queue_horizon_seconds × tick rate; over this the server answers queue_too_long.
QUEUE_HORIZON_INTENTS = DEFAULT_QUEUE_HORIZON_SECONDS * DEFAULT_TICK_RATE_HZ


def queue_horizon_intents(
    *,
    tick_rate_hz: int = DEFAULT_TICK_RATE_HZ,
    horizon_seconds: int = DEFAULT_QUEUE_HORIZON_SECONDS,
) -> int:
    """How many intents one POST may carry for a world with the given clock."""
    return tick_rate_hz * horizon_seconds
