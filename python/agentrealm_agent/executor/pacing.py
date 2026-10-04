"""Paced Step/Wait queues for movement (PLAYABLE_AGENT_PLAN Executor)."""

from __future__ import annotations

from ..world import Pos
from .intents import IntentQueue, StepDirection, step, wait
from .queue import trim_to_horizon

_DIRECTIONS: dict[tuple[int, int], StepDirection] = {
    (0, -1): "up",
    (0, 1): "down",
    (-1, 0): "left",
    (1, 0): "right",
    (-1, -1): "up_left",
    (1, -1): "up_right",
    (-1, 1): "down_left",
    (1, 1): "down_right",
}


def ticks_per_move(tick_hz: int, movement_speed: int) -> int:
    """Sim ticks between moves at this speed (API Movement, GAME_NOTES)."""
    speed = max(1, movement_speed)
    return max(1, round(tick_hz * 1000 / speed))


def step_direction(frm: Pos, to: Pos) -> StepDirection:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    try:
        return _DIRECTIONS[(dx, dy)]
    except KeyError as e:
        raise ValueError(f"not a one-step move from {frm} to {to}") from e


def step_landing(frm: Pos, direction: str) -> Pos:
    for delta, name in _DIRECTIONS.items():
        if name == direction:
            return (frm[0] + delta[0], frm[1] + delta[1])
    raise ValueError(f"unknown direction {direction!r}")


def pace_steps(
    start: Pos,
    steps: list[Pos],
    *,
    movement_speed: int,
    tick_hz: int,
    horizon_ticks: int,
) -> tuple[IntentQueue, list[Pos]]:
    """A Step, Wait×n, Step, … queue for `steps`, cut at the world's horizon.

    Returns the queue and the cells whose Step made it in.
    """
    waits = ticks_per_move(tick_hz, movement_speed) - 1
    intents: IntentQueue = []
    pos = start
    for i, target in enumerate(steps):
        if i:
            intents.extend(wait() for _ in range(waits))
        intents.append(step(step_direction(pos, target)))
        pos = target
    intents = trim_to_horizon(intents, limit=horizon_ticks)
    # Waits left after the last Step that fits would only idle; drop them.
    while intents and intents[-1]["verb"] == "Wait":
        intents.pop()
    queued = steps[: sum(1 for i in intents if i["verb"] == "Step")]
    return intents, queued


def movement_steps(path: list[Pos], target: Pos) -> list[Pos]:
    """Steps to queue: the path when it starts at `target`, else one step."""
    if path and path[0] == target:
        return list(path)
    return [target]
