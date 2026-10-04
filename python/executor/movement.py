"""Paced Step / Wait queues from a path and movement speed (M6)."""

from __future__ import annotations

from collections.abc import Sequence

Pos = tuple[int, int]

# API Movement: `up` is toward row 0 (decreasing y).
_DIRECTION_BY_DELTA: dict[tuple[int, int], str] = {
    (0, -1): "up",
    (0, 1): "down",
    (-1, 0): "left",
    (1, 0): "right",
    (-1, -1): "up_left",
    (1, -1): "up_right",
    (-1, 1): "down_left",
    (1, 1): "down_right",
}


def direction_between(frm: Pos, to: Pos) -> str:
    """Chebyshev-one step direction from ``frm`` to ``to`` (docs/API.md Step)."""
    dx = to[0] - frm[0]
    dy = to[1] - frm[1]
    if dx not in (-1, 0, 1) or dy not in (-1, 0, 1) or (dx, dy) == (0, 0):
        raise ValueError(f"not a one-block step: {frm!r} -> {to!r}")
    try:
        return _DIRECTION_BY_DELTA[dx, dy]
    except KeyError as e:
        raise ValueError(f"not a one-block step: {frm!r} -> {to!r}") from e


def step_intent(direction: str) -> dict:
    return {"verb": "Step", "direction": direction}


def wait_intent() -> dict:
    return {"verb": "Wait"}


def ticks_per_step(*, tick_rate_hz: int, movement_speed: float) -> int:
    """Ticks between allowed moves at ``movement_speed`` blocks per second.

    Matches the API movement accumulator: cost ``1000 * tick_rate_hz`` thousandths,
    adding ``movement_speed * 1000`` each tick (docs/API.md Movement).
    """
    if tick_rate_hz <= 0:
        raise ValueError("tick_rate_hz must be positive")
    if movement_speed <= 0:
        raise ValueError("movement_speed must be positive")
    cost = 1000 * tick_rate_hz
    speed = max(1, int(round(movement_speed * 1000)))
    return (cost + speed - 1) // speed


def build_paced_walk_queue(
    from_cell: Pos,
    cells: Sequence[Pos],
    *,
    movement_speed: float,
    tick_rate_hz: int = 10,
) -> list[dict]:
    """Turn a one-block path into ``Step``, ``Wait``×n, … intents.

    ``cells`` lists each block to enter in order; ``from_cell`` is where the
    character stands before the first ``Step``. At the default 2.5 blocks/s and
    10 Hz that is ``Step``, three ``Wait``s, then the next ``Step``.
    """
    if not cells:
        return []
    period = ticks_per_step(tick_rate_hz=tick_rate_hz, movement_speed=movement_speed)
    waits_after_step = max(0, period - 1)

    queue: list[dict] = []
    prev = from_cell
    for i, target in enumerate(cells):
        queue.append(step_intent(direction_between(prev, target)))
        prev = target
        if waits_after_step and i < len(cells) - 1:
            queue.extend(wait_intent() for _ in range(waits_after_step))
    return queue
