"""Paced Step / Wait queues from a path and movement speed (M6)."""

from __future__ import annotations

from collections.abc import Sequence

from .constants import DEFAULT_TICK_RATE_HZ
from .intents import IntentQueue, StepDirection, step, wait

Pos = tuple[int, int]

# API Movement: `up` is toward row 0 (decreasing y).
_DIRECTION_BY_DELTA: dict[tuple[int, int], StepDirection] = {
    (0, -1): "up",
    (0, 1): "down",
    (-1, 0): "left",
    (1, 0): "right",
    (-1, -1): "up_left",
    (1, -1): "up_right",
    (-1, 1): "down_left",
    (1, 1): "down_right",
}


def direction_between(frm: Pos, to: Pos) -> StepDirection:
    """Chebyshev-one step direction from ``frm`` to ``to`` (docs/API.md Step)."""
    direction = _DIRECTION_BY_DELTA.get((to[0] - frm[0], to[1] - frm[1]))
    if direction is None:
        raise ValueError(f"not a one-block step: {frm!r} -> {to!r}")
    return direction


def ticks_per_step(*, tick_rate_hz: int, movement_speed_milli: int) -> int:
    """Ticks between allowed moves at ``movement_speed_milli``.

    The speed is the API's integer in thousandths of a block per second (2500
    is 2.5 blocks/s). Matches the API movement accumulator: a move costs
    ``1000 * tick_rate_hz`` and each tick adds the speed (docs/API.md Movement).
    Rounds up, so an uneven speed waits one tick long rather than one short.
    """
    if tick_rate_hz <= 0:
        raise ValueError("tick_rate_hz must be positive")
    if movement_speed_milli <= 0:
        raise ValueError("movement_speed_milli must be positive")
    cost = 1000 * tick_rate_hz
    return -(-cost // movement_speed_milli)


def build_paced_walk_queue(
    from_cell: Pos,
    cells: Sequence[Pos],
    *,
    movement_speed_milli: int,
    tick_rate_hz: int = DEFAULT_TICK_RATE_HZ,
    ticks_since_last_step: int | None = None,
) -> IntentQueue:
    """Turn a one-block path into ``Step``, ``Wait``×n, … intents.

    ``cells`` lists each block to enter in order; ``from_cell`` is where the
    character stands before the first ``Step``. At 2500 and 10 Hz that is
    ``Step``, three ``Wait``s, then the next ``Step``.

    ``ticks_since_last_step`` keeps consecutive queues paced: it is how many
    ticks after the last applied ``Step`` this queue's first intent runs (1
    for the tick right after it). The queue then opens with the ``Wait``s still
    owed, so it never draws ``movement_cooldown``. ``None`` means no recent
    step: the accumulator is full and the first ``Step`` goes at once.
    """
    period = ticks_per_step(
        tick_rate_hz=tick_rate_hz, movement_speed_milli=movement_speed_milli
    )
    if ticks_since_last_step is not None and ticks_since_last_step < 1:
        raise ValueError("ticks_since_last_step must be at least 1")
    if not cells:
        return []

    queue: IntentQueue = []
    if ticks_since_last_step is not None:
        queue.extend(wait() for _ in range(max(0, period - ticks_since_last_step)))
    prev = from_cell
    for i, target in enumerate(cells):
        if i:
            queue.extend(wait() for _ in range(period - 1))
        queue.append(step(direction_between(prev, target)))
        prev = target
    return queue
