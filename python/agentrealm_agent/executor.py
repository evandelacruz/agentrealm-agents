"""M6: paced Step/Wait queues for movement (PLAYABLE_AGENT_PLAN Executor)."""

from __future__ import annotations

from .world import Pos

DEFAULT_MOVEMENT_SPEED = 2500  # thousandths of a block per second (API default pace)
DEFAULT_QUEUE_HORIZON_SECONDS = 4

_DIRECTIONS: dict[tuple[int, int], str] = {
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


def step_direction(frm: Pos, to: Pos) -> str:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    try:
        return _DIRECTIONS[(dx, dy)]
    except KeyError as e:
        raise ValueError(f"not a one-step move from {frm} to {to}") from e


def pace_steps(
    start: Pos,
    steps: list[Pos],
    *,
    movement_speed: int,
    tick_hz: int,
    horizon_ticks: int,
) -> tuple[list[dict], list[Pos]]:
    """Build a Step/Wait queue for `steps`, capped at the world's tick horizon."""
    if not steps:
        return [], []
    wait = {"verb": "Wait"}
    waits = ticks_per_move(tick_hz, movement_speed) - 1
    between = [wait] * waits if waits else []

    intents: list[dict] = []
    queued: list[Pos] = []
    pos = start
    for i, target in enumerate(steps):
        chunk = 1 + (len(between) if i < len(steps) - 1 else 0)
        if len(intents) + chunk > horizon_ticks:
            break
        intents.append({"verb": "Step", "direction": step_direction(pos, target)})
        if i < len(steps) - 1:
            intents.extend(between)
        queued.append(target)
        pos = target
    return intents, queued


def step_landing(frm: Pos, direction: str) -> Pos:
    for delta, name in _DIRECTIONS.items():
        if name == direction:
            return (frm[0] + delta[0], frm[1] + delta[1])
    raise ValueError(f"unknown direction {direction!r}")


def movement_steps(path: list[Pos], target: Pos) -> list[Pos]:
    """Steps to queue: the path prefix when it starts at `target`, else one step."""
    if path and path[0] == target:
        return list(path)
    return [target]
