"""M6 executor helpers: paced intent queues for attacks, speech, and combat tails.

Paths and poll cadences live in later slices; this module covers weapon-cooldown
spacing for ``Use``, speech spacing for ``Say``/``Broadcast``, and bounding
attack queues to the next poll so a slow round trip does not keep swinging.
"""

from __future__ import annotations

from typing import Callable, Iterable

DEFAULT_WEAPON_COOLDOWN_TICKS = 10
SPEECH_INTERVAL_TICKS = 10
QUEUE_HORIZON_TICKS = 40


def wait() -> dict:
    return {"verb": "Wait"}


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
) -> list[dict]:
    """Insert ``Wait`` intents so consecutive cooldown actions are ``interval_ticks`` apart."""
    if interval_ticks <= 1 or not actions:
        return list(actions)
    gap = interval_ticks - 1
    out: list[dict] = []
    saw_cooldown = False
    for intent in actions:
        if saw_cooldown and spends_cooldown(intent):
            out.extend(waits(gap))
        out.append(intent)
        if spends_cooldown(intent):
            saw_cooldown = True
    return out


def pace_uses(
    uses: Iterable[dict],
    *,
    cooldown_ticks: int = DEFAULT_WEAPON_COOLDOWN_TICKS,
) -> list[dict]:
    """Space ``Use`` intents by weapon cooldown (10 ticks by default)."""
    return _pace_actions(list(uses), interval_ticks=cooldown_ticks, spends_cooldown=uses_attack_cooldown)


def pace_speech(
    lines: Iterable[dict],
    *,
    interval_ticks: int = SPEECH_INTERVAL_TICKS,
) -> list[dict]:
    """Space ``Say`` and ``Broadcast`` intents by the speech interval (10 ticks by default)."""
    return _pace_actions(list(lines), interval_ticks=interval_ticks, spends_cooldown=uses_speech_cooldown)


def build_attack_queue(
    uses: Iterable[dict],
    retreat: Iterable[dict] | None = None,
    *,
    poll_interval_ticks: int,
    weapon_cooldown_ticks: int = DEFAULT_WEAPON_COOLDOWN_TICKS,
    horizon_ticks: int = QUEUE_HORIZON_TICKS,
) -> list[dict]:
    """Paced attacks with an optional retreat tail, capped for the next poll.

    A slow poll must not leave the character swinging after the fight turned.
    When the paced attacks plus retreat exceed the poll window (or queue
    horizon), attacks drop from the end until the queue fits, keeping the
    retreat tail when possible.
    """
    if poll_interval_ticks <= 0:
        raise ValueError("poll_interval_ticks must be positive")
    retreat_list = list(retreat or [])
    paced = pace_uses(uses, cooldown_ticks=weapon_cooldown_ticks)
    limit = min(horizon_ticks, poll_interval_ticks)
    if not paced and not retreat_list:
        return []
    if len(paced) + len(retreat_list) <= limit:
        return paced + retreat_list
    retreat_keep = min(len(retreat_list), limit)
    attack_budget = limit - retreat_keep
    if attack_budget <= 0:
        return retreat_list[:limit]
    return paced[:attack_budget] + retreat_list[:retreat_keep]
