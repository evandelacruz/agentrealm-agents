"""M6 executor helpers: paced intent queues for attacks, speech, and combat tails.

Paths and poll cadences live in later slices; this module covers weapon-cooldown
spacing for ``Use``, speech spacing for ``Say``/``Broadcast``, and bounding
attack queues to the next poll so a slow round trip does not keep swinging.

The attack and speech accumulators are separate: pace a homogeneous list with
``pace_uses`` or ``pace_speech``. There is no mixed-queue helper yet. Pacing
carries across queues through ``ticks_since_last``.
"""

from __future__ import annotations

from typing import Callable, Iterable

from .intents import wait

DEFAULT_WEAPON_COOLDOWN_TICKS = 10
SPEECH_INTERVAL_TICKS = 10


def waits(count: int) -> list[dict]:
    if count <= 0:
        return []
    return [wait() for _ in range(count)]


def uses_attack_cooldown(intent: dict) -> bool:
    return intent.get("verb") == "Use"


def uses_speech_cooldown(intent: dict) -> bool:
    return intent.get("verb") in ("Say", "Broadcast")


def _pace_actions(
    actions: list[dict],
    *,
    interval_ticks: int,
    spends_cooldown: Callable[[dict], bool],
    ticks_since_last: int | None,
) -> list[dict]:
    """Insert ``Wait`` intents so cooldown actions land ``interval_ticks`` apart.

    ``ticks_since_last`` is how many ticks before the queue's first slot the
    last cooldown action ran (1 means the tick just before), or ``None`` when
    the accumulator is already full. Other intents in between count toward the
    gap.
    """
    ready_at = 0 if ticks_since_last is None else max(0, interval_ticks - ticks_since_last)
    out: list[dict] = []
    for intent in actions:
        if spends_cooldown(intent):
            out.extend(waits(ready_at - len(out)))
            ready_at = len(out) + interval_ticks
        out.append(intent)
    return out


def pace_uses(
    uses: Iterable[dict],
    *,
    cooldown_ticks: int = DEFAULT_WEAPON_COOLDOWN_TICKS,
    ticks_since_last: int | None = None,
) -> list[dict]:
    """Space ``Use`` intents by weapon cooldown (10 ticks by default)."""
    return _pace_actions(
        list(uses),
        interval_ticks=cooldown_ticks,
        spends_cooldown=uses_attack_cooldown,
        ticks_since_last=ticks_since_last,
    )


def pace_speech(
    lines: Iterable[dict],
    *,
    interval_ticks: int = SPEECH_INTERVAL_TICKS,
    ticks_since_last: int | None = None,
) -> list[dict]:
    """Space ``Say`` and ``Broadcast`` intents by the speech interval (10 ticks by default)."""
    return _pace_actions(
        list(lines),
        interval_ticks=interval_ticks,
        spends_cooldown=uses_speech_cooldown,
        ticks_since_last=ticks_since_last,
    )


def build_attack_queue(
    uses: Iterable[dict],
    retreat: Iterable[dict] | None = None,
    *,
    poll_interval_ticks: int,
    horizon_ticks: int,
    weapon_cooldown_ticks: int = DEFAULT_WEAPON_COOLDOWN_TICKS,
    ticks_since_last_use: int | None = None,
) -> list[dict]:
    """Paced attacks with an optional retreat tail, capped for the next poll.

    A slow poll must not leave the character swinging after the fight turned.
    When the paced attacks plus retreat exceed the poll window (or queue
    horizon), attacks drop from the end until the queue fits, keeping the
    retreat tail when possible. Trailing ``Wait``s left by the cut are
    dropped too, so the retreat starts on the tick after the last swing.
    ``horizon_ticks`` comes from the world (``queue_horizon_intents``).
    """
    if poll_interval_ticks <= 0:
        raise ValueError("poll_interval_ticks must be positive")
    retreat_list = list(retreat or [])
    paced = pace_uses(uses, cooldown_ticks=weapon_cooldown_ticks, ticks_since_last=ticks_since_last_use)
    limit = min(horizon_ticks, poll_interval_ticks)
    if not paced and not retreat_list:
        return []
    if len(paced) + len(retreat_list) <= limit:
        return paced + retreat_list
    retreat_keep = min(len(retreat_list), limit)
    attack_budget = limit - retreat_keep
    attacks = paced[:attack_budget]
    while attacks and attacks[-1] == wait():
        attacks.pop()
    return attacks + retreat_list[:retreat_keep]
