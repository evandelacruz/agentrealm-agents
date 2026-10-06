"""Travel destinations (A27): ``travel:*`` directives shorthand and plan ``travel`` ops."""

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


def travel_op_from_plan_goal(op: dict[str, Any]) -> TravelOp:
    """Build a ``TravelOp`` from a validated plan ``travel`` goal (A34).

    For ``shop`` and ``entrance``, ``x=0`` and ``y=0`` mean the nearest known
    one (like ``travel:shop`` in directives), not the map origin. ``town`` and
    ``hunting_ground`` take no coordinates.
    """
    to = op["to"]
    x, y = op["x"], op["y"]
    map_id = op.get("map_id")
    if to in ("town", "hunting_ground") or (to in ("shop", "entrance") and x == 0 and y == 0):
        return TravelOp(to=to, map_id=map_id if isinstance(map_id, int) else None)
    return TravelOp(
        to=to,
        x=x,
        y=y,
        map_id=map_id if isinstance(map_id, int) else None,
    )


def point_dest(op: dict[str, Any], map_id: int | None) -> tuple[int | None, tuple[int, int]] | None:
    """The map and cell a ``travel`` op to a ``point`` walks to (its own
    ``map_id``, else ``map_id``, the current map), or None for any other op.
    Stuck detection's give-ups are matched against this (A16)."""
    if op.get("op") != "travel" or op.get("to") != "point":
        return None
    mid = op.get("map_id")
    return (mid if isinstance(mid, int) else map_id, (op["x"], op["y"]))


def _opt_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
