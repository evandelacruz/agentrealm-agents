"""Pure helpers for intent queues before the runner sends them."""

from __future__ import annotations

from collections.abc import Sequence

from ..client import Intent
from .intents import IntentQueue


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError(f"limit must be a non-negative int, got {limit!r}")


def trim_to_horizon(queue: Sequence[Intent], *, limit: int) -> IntentQueue:
    """Keep the prefix that fits the world's queue horizon; drop the rest.

    `limit` comes from queue_horizon_intents for the world being played.
    """
    _check_limit(limit)
    return list(queue[:limit])


def within_horizon(queue: Sequence[Intent], *, limit: int) -> bool:
    """True when posting the whole queue would not hit queue_too_long."""
    _check_limit(limit)
    return len(queue) <= limit
