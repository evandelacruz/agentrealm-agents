"""Limits shared with the Agent Realm intent queue (manual §7.6, GAME_NOTES)."""

from __future__ import annotations

DEFAULT_TICK_RATE_HZ = 10
DEFAULT_QUEUE_HORIZON_SECONDS = 4

# The horizon at the defaults. Callers that know the world's clock use queue_horizon_intents.
QUEUE_HORIZON_INTENTS = DEFAULT_QUEUE_HORIZON_SECONDS * DEFAULT_TICK_RATE_HZ


def queue_horizon_intents(
    *,
    tick_rate_hz: int = DEFAULT_TICK_RATE_HZ,
    horizon_seconds: int = DEFAULT_QUEUE_HORIZON_SECONDS,
) -> int:
    """How many intents one POST may carry: queue_horizon_seconds × tick rate.

    Over this the server answers queue_too_long.
    """
    for name, value in (("tick_rate_hz", tick_rate_hz), ("horizon_seconds", horizon_seconds)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive int, got {value!r}")
    return tick_rate_hz * horizon_seconds
