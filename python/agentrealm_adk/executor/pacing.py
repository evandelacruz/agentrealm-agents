"""M6 executor helpers: paced intent queues for attacks, speech, and combat tails.

Paths and poll cadences live in later slices; this module covers weapon-cooldown
spacing for ``Use``, speech spacing for ``Say``/``Broadcast``, and bounding
attack queues to the next poll so a slow round trip does not keep swinging.

The attack and speech accumulators are separate: pace a homogeneous list with
``pace_uses`` or ``pace_speech``. There is no mixed-queue helper yet. Pacing
carries across queues through ``ticks_since_last``.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable

from ..supplies import DEFAULT_ATTACK_COOLDOWN_MS, row, weapon_cooldown_ms
from .constants import DEFAULT_TICK_RATE_HZ
from .intents import wait

SPEECH_INTERVAL_TICKS = 10


def waits(count: int) -> list[dict]:
    if count <= 0:
        return []
    return [wait() for _ in range(count)]


def attack_cooldown_ticks(code: str | None, tick_rate_hz: int = DEFAULT_TICK_RATE_HZ) -> int:
    """Ticks between two swings with ``code`` armed: a weapon's attack
    cooldown (the Supplies reference's ``attacks`` ``cooldown_ms``, A123; 0.8 s
    to 1.5 s in Olympuff), the 1 s default for anything else, rounded up to a
    whole tick. Only a weapon swing waits on it (``is_weapon_swing``); pace a
    ``Use`` by ``use_cooldown_ticks``."""
    cooldown_ms = weapon_cooldown_ms(code) or DEFAULT_ATTACK_COOLDOWN_MS
    return max(1, math.ceil(cooldown_ms * tick_rate_hz / 1000))


# The default attack cooldown at the default tick rate: a time or cost
# measured in swings, never a swing's pacing, which reads the armed weapon's
# (``attack_cooldown_ticks``).
DEFAULT_WEAPON_COOLDOWN_TICKS = attack_cooldown_ticks(None)


def is_weapon_swing(intent: dict, code: str | None) -> bool:
    """Whether ``intent`` is a weapon's swing: a ``Use`` with ``code`` in hand
    on a character, an NPC or a block (Manual §11 What waits on a cooldown:
    only a weapon ``Use`` that rolls against a character or NPC, or breaks a
    block). A ``Use`` on yourself (a drink, a chest) and any ``Use`` of a
    listed consumable, tool or trap (matches, a bomb) is no swing. Nothing in
    hand, or a code the Supplies reference does not list, counts as a weapon:
    a free ``Use`` paced costs a tick, a swing sent early costs a refusal."""
    if intent.get("verb") != "Use" or (intent.get("target") or {}).get("kind") == "self":
        return False
    r = row(code)
    return r is None or r.supply_class == "weapon"


def use_cooldown_ticks(intent: dict, code: str | None, tick_rate_hz: int = DEFAULT_TICK_RATE_HZ) -> int:
    """Ticks a ``Use`` made with ``code`` in hand waits after the last swing:
    the weapon's attack cooldown for a swing (``is_weapon_swing``), else 0,
    so a drink, a chest, a tool or a trap goes out on the next tick."""
    return attack_cooldown_ticks(code, tick_rate_hz) if is_weapon_swing(intent, code) else 0


def uses_attack_cooldown(intent: dict) -> bool:
    """A ``Use`` that ``pace_uses`` spaces: every ``Use`` but one on yourself.
    Whether it waits at all is its cooldown's (``use_cooldown_ticks``)."""
    return intent.get("verb") == "Use" and (intent.get("target") or {}).get("kind") != "self"


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
    cooldown_ticks: int,
    ticks_since_last: int | None = None,
) -> list[dict]:
    """Space ``Use`` intents ``cooldown_ticks`` apart: the armed weapon's
    attack cooldown at the world's tick rate for swings, 0 for a ``Use`` that
    waits on none (``use_cooldown_ticks``)."""
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


def arm_then_use(
    arm: dict,
    use: dict,
    *,
    cooldown_ticks: int,
    horizon_ticks: int,
    ticks_since_last_use: int | None = None,
) -> list[dict]:
    """``Arm`` then ``Use`` in one queue, the ``Use`` behind the Waits its
    cooldown still owes, cut at the horizon. ``cooldown_ticks`` is the
    ``Use``'s with what the ``Arm`` puts in hand (``use_cooldown_ticks``):
    0 for a drink, a chest, a tool or a trap, so both go out on the next
    ticks; the ``Arm`` itself never waits.

    A state that returns the two unpaced has only its first intent sent
    (``brain.decide``): the ``Arm`` goes out and the ``Use`` never does. So
    the two go together or not at all: when the horizon would cut the
    ``Use``, the queue is empty and the caller waits for the cooldown.
    """
    queue = pace_uses([arm, use], cooldown_ticks=cooldown_ticks, ticks_since_last=ticks_since_last_use)
    return queue if len(queue) <= horizon_ticks else []


def build_attack_queue(
    uses: Iterable[dict],
    retreat: Iterable[dict] | None = None,
    *,
    poll_interval_ticks: int,
    horizon_ticks: int,
    weapon_cooldown_ticks: int,
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
