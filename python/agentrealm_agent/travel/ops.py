"""Travel plan operations (A27). Full plan schema is A34; this slice parses travel ops only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TRAVEL_KINDS = ("entrance", "town", "hunting_ground", "shop", "point")


@dataclass(frozen=True)
class TravelOp:
    to: str
    x: int | None = None
    y: int | None = None
    map_id: int | None = None
    why: str = ""


def parse_travel_string(goal: str) -> TravelOp | None:
    """``travel:<to>``, ``travel:<to>:x:y`` or ``travel:<to>:map_id:x:y``.

    ``town`` and ``hunting_ground`` take no coordinates, ``entrance`` and
    ``point`` need them, ``shop`` takes either. Anything else is ignored.
    """
    parts = goal.split(":")
    if len(parts) < 2 or parts[0] != "travel" or parts[1] not in TRAVEL_KINDS:
        return None
    to = parts[1]
    nums = [_opt_int(p) for p in parts[2:]]
    if any(n is None for n in nums):
        return None
    if not nums:
        return None if to in ("entrance", "point") else TravelOp(to=to)
    if to in ("town", "hunting_ground"):
        return None
    if len(nums) == 2:
        return TravelOp(to=to, x=nums[0], y=nums[1])
    if len(nums) == 3:
        return TravelOp(to=to, x=nums[1], y=nums[2], map_id=nums[0])
    return None


def parse_travel_goals(goals: list[str]) -> list[TravelOp]:
    out: list[TravelOp] = []
    for g in goals:
        op = parse_travel_string(g)
        if op is not None:
            out.append(op)
    return out


def refresh_travel_stack(memory, goals: list[str]) -> None:
    """Replace the travel queue when directives goals change."""
    parsed = parse_travel_goals(goals)
    if parsed != memory.travel_ops:
        memory.travel_ops = parsed
        memory.travel_index = 0


def current_travel_op(memory) -> TravelOp | None:
    if memory.travel_index >= len(memory.travel_ops):
        return None
    return memory.travel_ops[memory.travel_index]


def set_travel_index(memory, index: int) -> None:
    """Move the stack to ``index``; ops before it are dropped (arrived or unresolved)."""
    if index != memory.travel_index:
        memory.travel_index = index
        memory.path, memory.goal = [], ""


def travel_op_from_plan_goal(op: dict[str, Any]) -> TravelOp:
    """Build a ``TravelOp`` from a validated plan ``travel`` goal (A34).

    For ``shop``, ``x=0`` and ``y=0`` mean any known shop cell (like
    ``travel:shop`` in directives), not the map origin.
    """
    to = op["to"]
    x, y = op["x"], op["y"]
    map_id = op.get("map_id")
    if to == "shop" and x == 0 and y == 0:
        return TravelOp(to="shop", map_id=map_id if isinstance(map_id, int) else None)
    return TravelOp(
        to=to,
        x=x,
        y=y,
        map_id=map_id if isinstance(map_id, int) else None,
    )


def _opt_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
