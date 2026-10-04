"""Travel plan operations (A27). Full plan schema is A34; this slice parses travel ops only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..world import Pos

TRAVEL_KINDS = ("entrance", "town", "hunting_ground", "shop", "point")


@dataclass(frozen=True)
class TravelOp:
    to: str
    x: int | None = None
    y: int | None = None
    map_id: int | None = None
    why: str = ""


def parse_travel_dict(raw: dict[str, Any]) -> TravelOp | None:
    if raw.get("op") != "travel":
        return None
    to = raw.get("to")
    if to not in TRAVEL_KINDS:
        return None
    x, y = _opt_int(raw.get("x")), _opt_int(raw.get("y"))
    if to in ("entrance", "point", "shop") and (x is None or y is None):
        if to != "shop":
            return None
    if to == "shop" and x is not None and y is None:
        return None
    return TravelOp(to=str(to), x=x, y=y, map_id=_opt_int(raw.get("map_id")), why=str(raw.get("why") or ""))


def parse_travel_string(goal: str) -> TravelOp | None:
    """``travel:<to>[:map_id:x:y]`` or ``travel:<to>:x:y`` from directives goals."""
    if not goal.startswith("travel:"):
        return None
    parts = goal.split(":")
    if len(parts) < 2:
        return None
    to = parts[1]
    if to not in TRAVEL_KINDS:
        return None
    nums = [_opt_int(p) for p in parts[2:]]
    nums = [n for n in nums if n is not None]
    map_id = x = y = None
    if to in ("town", "hunting_ground"):
        pass
    elif to == "shop" and not nums:
        pass
    elif len(nums) == 2:
        x, y = nums
    elif len(nums) == 3:
        map_id, x, y = nums
    elif to in ("entrance", "point"):
        return None
    return TravelOp(to=to, x=x, y=y, map_id=map_id)


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


def advance_travel_op(memory) -> None:
    memory.travel_index += 1
    memory.path, memory.goal = [], ""


def _opt_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
