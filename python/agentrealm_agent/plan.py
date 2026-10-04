"""Plan schema, goal stack, and built-in planning without a model (A34)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Policy
from .directives import PARAM_DEFAULTS, _valid_param
from .executor.constants import DEFAULT_TICK_RATE_HZ
from .world import DOORS, Pos, WorldModel, chebyshev

log = logging.getLogger(__name__)

GoalOp = dict[str, Any]

TRAVEL_TO = frozenset({"entrance", "town", "hunting_ground", "shop", "point"})
CAPABILITIES = frozenset({"cut", "chop", "smash", "burn", "blast"})
ALL_PARAMS = frozenset(PARAM_DEFAULTS)

# Op name -> owning state (docs/PLAYABLE_AGENT_PLAN.md Operations table).
OP_STATE: dict[str, str | None] = {
    "travel": "Travel",
    "explore_area": "Explore",
    "read": "Investigate",
    "say": "Investigate",
    "buy": "Shop",
    "break_block": "Break",
    "use_block": "Solve",
    "compose": "Solve",
    "fetch_item": "Loot",
    "gather_gems": "Gather",
    "hunt": "Fight",
    "enter_level": "Level",
    "fight_boss": "Boss",
    "avoid": None,
    "wait": "Idle",
    "set_param": None,
}

# Ops the shipped Explore pathing can drive today. Every other op is dropped
# with a log line when it reaches the top of the stack (A34 slice).
EXPLORE_PATH_OPS = frozenset({"explore_area", "travel", "wait"})
BOSS_PLAN_OPS = frozenset({"fight_boss"})
# `travel` destinations with a path today; `hunting_ground` and `shop` wait on A20/Shop.
TRAVEL_PATHED = frozenset({"entrance", "town", "point"})

# Built-in `explore` explores the whole map: no center, no radius bound.
EXPLORE_ANYWHERE = 1 << 30
# Built-in `hold`: one hour, re-entered from policy.goals when it ends.
HOLD_SECONDS = 3600
# An op that finds no path for this long is dropped and logged.
PLAN_STALL_SECONDS = 30

# Shorthand in directives `goals = ["gather_gems:20", "buy:torch"]`.
_SHORTHAND = re.compile(r"^([a-z_]+):(.+)$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _drop(reason: str, op: object) -> None:
    log.warning("plan: dropped op %r: %s", op, reason)


def _require_fields(op: dict[str, Any], fields: tuple[str, ...]) -> bool:
    for key in fields:
        if key not in op:
            _drop(f"missing `{key}`", op)
            return False
    return True


def _validate_travel(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("to", "x", "y")):
        return False
    if not _is_str(op["to"]) or op["to"] not in TRAVEL_TO:
        _drop("bad `to`", op)
        return False
    if not _is_int(op["x"]) or not _is_int(op["y"]):
        _drop("bad coordinates", op)
        return False
    if "map_id" in op and not _is_int(op["map_id"]):
        _drop("bad `map_id`", op)
        return False
    return True


def _validate_explore_area(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("x", "y", "radius")):
        return False
    if not all(_is_int(op[k]) for k in ("x", "y", "radius")) or op["radius"] < 0:
        _drop("bad explore_area geometry", op)
        return False
    return True


def _validate_read(op: dict[str, Any]) -> bool:
    has_xy = "x" in op and "y" in op
    has_supply = "supply_id" in op
    if has_xy == has_supply:
        _drop("read needs x,y or supply_id", op)
        return False
    if has_xy and (not _is_int(op["x"]) or not _is_int(op["y"])):
        _drop("bad read coordinates", op)
        return False
    if has_supply and not _is_int(op["supply_id"]):
        _drop("bad supply_id", op)
        return False
    return True


def _validate_say(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("text",)):
        return False
    if not _is_str(op["text"]):
        _drop("bad text", op)
        return False
    has_id = "npc_id" in op
    has_type = "npc_type" in op
    if has_id == has_type:
        _drop("say needs npc_id or npc_type", op)
        return False
    if has_id and not _is_int(op["npc_id"]):
        _drop("bad npc_id", op)
        return False
    if has_type and not _is_str(op["npc_type"]):
        _drop("bad npc_type", op)
        return False
    return True


def _validate_buy(op: dict[str, Any]) -> bool:
    return _require_fields(op, ("code",)) and _is_str(op["code"])


def _validate_break_block(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("x", "y", "capability")):
        return False
    if not all(_is_int(op[k]) for k in ("x", "y")):
        _drop("bad break_block coordinates", op)
        return False
    if op["capability"] not in CAPABILITIES:
        _drop("bad capability", op)
        return False
    return True


def _validate_use_block(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("x", "y", "code")):
        return False
    if not all(_is_int(op[k]) for k in ("x", "y")) or not _is_str(op["code"]):
        _drop("bad use_block fields", op)
        return False
    return True


def _validate_compose(op: dict[str, Any]) -> bool:
    return _require_fields(op, ("composes_into",)) and _is_str(op["composes_into"])


def _validate_fetch_item(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("code",)) or not _is_str(op["code"]):
        return False
    if "x" in op or "y" in op:
        if not _is_int(op.get("x")) or not _is_int(op.get("y")):
            _drop("bad fetch_item coordinates", op)
            return False
    return True


def _validate_gather_gems(op: dict[str, Any]) -> bool:
    return _require_fields(op, ("count",)) and _is_int(op["count"]) and op["count"] >= 0


def _validate_hunt(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("npc_type",)) or not _is_str(op["npc_type"]):
        return False
    if "x" in op or "y" in op:
        if not _is_int(op.get("x")) or not _is_int(op.get("y")):
            _drop("bad hunt coordinates", op)
            return False
    return True


def _validate_enter_level(op: dict[str, Any]) -> bool:
    return _require_fields(op, ("x", "y")) and all(_is_int(op[k]) for k in ("x", "y"))


def _validate_fight_boss(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("x", "y")) or not all(_is_int(op[k]) for k in ("x", "y")):
        return False
    if "min_health" in op and (not _is_int(op["min_health"]) or op["min_health"] < 0):
        _drop("bad min_health", op)
        return False
    if "min_potions" in op and (not _is_int(op["min_potions"]) or op["min_potions"] < 0):
        _drop("bad min_potions", op)
        return False
    if "armed" in op and not _is_str(op["armed"]):
        _drop("bad armed", op)
        return False
    if "worn" in op:
        worn = op["worn"]
        if not isinstance(worn, list) or not worn or not all(_is_str(c) for c in worn):
            _drop("bad worn", op)
            return False
    return True


def _validate_avoid(op: dict[str, Any]) -> bool:
    has_npc = "npc_type" in op and _is_str(op["npc_type"])
    has_block = "block_type" in op and _is_str(op["block_type"])
    has_area = all(k in op for k in ("x", "y", "radius")) and all(_is_int(op[k]) for k in ("x", "y", "radius"))
    if sum(1 for x in (has_npc, has_block, has_area) if x) != 1:
        _drop("avoid needs exactly one of npc_type, block_type, or x,y,radius", op)
        return False
    if has_area and op["radius"] < 0:
        _drop("bad avoid radius", op)
        return False
    return True


def _validate_wait(op: dict[str, Any]) -> bool:
    return _require_fields(op, ("seconds",)) and _is_int(op["seconds"]) and op["seconds"] >= 0


def _validate_set_param(op: dict[str, Any]) -> bool:
    if not _require_fields(op, ("name", "value")):
        return False
    if op["name"] not in ALL_PARAMS:
        _drop("unknown param name", op)
        return False
    if op["name"] == "never_attack":
        _drop("never_attack is not settable", op)
        return False
    if _valid_param(op["name"], op["value"]) is None:
        _drop("param value out of range", op)
        return False
    return True


_VALIDATORS: dict[str, Any] = {
    "travel": _validate_travel,
    "explore_area": _validate_explore_area,
    "read": _validate_read,
    "say": _validate_say,
    "buy": _validate_buy,
    "break_block": _validate_break_block,
    "use_block": _validate_use_block,
    "compose": _validate_compose,
    "fetch_item": _validate_fetch_item,
    "gather_gems": _validate_gather_gems,
    "hunt": _validate_hunt,
    "enter_level": _validate_enter_level,
    "fight_boss": _validate_fight_boss,
    "avoid": _validate_avoid,
    "wait": _validate_wait,
    "set_param": _validate_set_param,
}


def validate_goal_op(raw: object) -> GoalOp | None:
    """Return a validated op dict, or None when it should be dropped."""
    if not isinstance(raw, dict):
        _drop("not an object", raw)
        return None
    op_name = raw.get("op")
    if not _is_str(op_name):
        _drop("missing or bad `op`", raw)
        return None
    if op_name not in _VALIDATORS:
        _drop("unknown op", raw)
        return None
    op = dict(raw)
    if "why" in op and not isinstance(op["why"], str):
        _drop("bad why", op)
        return None
    if not _VALIDATORS[op_name](op):
        return None
    return op


def is_travel_goal(text: str) -> bool:
    """``travel:*`` entries belong to **Travel** (A27), which keeps its own
    queue from directives ``goals``; the stack leaves them out so the two
    never walk the same destination with different progress."""
    return text.strip().startswith("travel:")


def parse_directives_goal(text: str) -> GoalOp | None:
    """Turn one directives shorthand string into a validated op.

    ``travel:*`` entries return None without a log line: Travel reads them.
    """
    text = text.strip()
    if not text or is_travel_goal(text):
        return None
    m = _SHORTHAND.match(text)
    if not m:
        _drop("bad shorthand", text)
        return None
    name, arg = m.group(1), m.group(2).strip()
    if name == "gather_gems":
        try:
            count = int(arg)
        except ValueError:
            _drop("bad gather_gems count", text)
            return None
        return validate_goal_op({"op": "gather_gems", "count": count})
    if name == "buy":
        return validate_goal_op({"op": "buy", "code": arg})
    _drop("unknown shorthand op", text)
    return None


def parse_directives_goals(lines: list[str]) -> list[GoalOp]:
    out: list[GoalOp] = []
    for line in lines:
        op = parse_directives_goal(line)
        if op is not None:
            out.append(op)
    return out


def parse_plan_payload(raw: object, *, floor_params: dict[str, float | int]) -> tuple[list[GoalOp], dict[str, float | int], str]:
    """Parse strategist JSON: goals, params, notes. Drops invalid ops and params."""
    if not isinstance(raw, dict):
        log.warning("plan: payload is not an object")
        return [], dict(floor_params), ""
    extra = set(raw) - {"goals", "params", "notes"}
    if extra:
        log.warning("plan: dropped unknown top-level keys %s", sorted(extra))
    goals_raw = raw.get("goals", [])
    if not isinstance(goals_raw, list):
        log.warning("plan: `goals` must be a list")
        goals_raw = []
    goals: list[GoalOp] = []
    for item in goals_raw:
        op = validate_goal_op(item)
        if op is not None:
            goals.append(op)
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        log.warning("plan: `notes` must be a string")
        notes = ""
    params = dict(floor_params)
    incoming = raw.get("params")
    if incoming is not None:
        if not isinstance(incoming, dict):
            log.warning("plan: `params` must be an object")
        else:
            params = apply_strategist_params(floor_params, params, incoming)
    return goals, params, notes


def apply_strategist_params(
    floor: dict[str, float | int],
    current: dict[str, float | int],
    incoming: dict[str, object],
) -> dict[str, float | int]:
    """Merge strategist params without loosening past the directives floor."""
    out = dict(current)
    for name, value in incoming.items():
        if name not in ALL_PARAMS:
            log.warning("plan: unknown param `%s` ignored", name)
            continue
        ok = _valid_param(name, value)
        if ok is None:
            log.warning("plan: param `%s`=%r out of range; ignored", name, value)
            continue
        if name == "curiosity":
            out[name] = ok
            continue
        floor_val = floor.get(name, PARAM_DEFAULTS[name])
        if name == "risk":
            if ok > floor_val:
                log.warning("plan: risk %s above floor %s; ignored", ok, floor_val)
                continue
            out[name] = ok
            continue
        # fight_margin, retreat_hits, lives_floor, potion_reserve: may only rise.
        if ok < floor_val:
            log.warning("plan: %s %s below floor %s; ignored", name, ok, floor_val)
            continue
        out[name] = ok
    return out


def apply_set_param(
    floor: dict[str, float | int], current: dict[str, float | int], op: GoalOp
) -> dict[str, float | int]:
    return apply_strategist_params(floor, current, {op["name"]: op["value"]})


@dataclass
class Plan:
    """Validated goal stack plus effective params (A34).

    ``params`` holds the effective survival params for the strategist; the
    survival states read the directives' params (A9). ``current`` and ``goal_done`` only read;
    ``advance`` and ``drop_current`` are the only calls that move the stack.
    """

    goals: list[GoalOp]
    params: dict[str, float | int]
    notes: str = ""
    index: int = 0
    floor_params: dict[str, float | int] = field(default_factory=lambda: dict(PARAM_DEFAULTS))
    wait_started_tick: int | None = None
    stalled_since_tick: int | None = None  # first tick the current op found no path
    tick_hz: int = DEFAULT_TICK_RATE_HZ  # world tick rate; converts `wait` seconds to ticks

    def snapshot(self) -> tuple:
        """The stack's progress, for :meth:`restore` after a side-effect-free probe."""
        return (
            self.index,
            list(self.goals),
            dict(self.params),
            dict(self.floor_params),
            self.wait_started_tick,
            self.stalled_since_tick,
        )

    def restore(self, saved: tuple) -> None:
        """Put back progress taken by :meth:`snapshot`."""
        (
            self.index,
            goals,
            params,
            floor_params,
            self.wait_started_tick,
            self.stalled_since_tick,
        ) = saved
        self.goals, self.params, self.floor_params = list(goals), dict(params), dict(floor_params)

    def current(self) -> GoalOp | None:
        """The op at the top of the stack, or None when it is empty."""
        return self.goals[self.index] if self.index < len(self.goals) else None

    def advance(self, world: WorldModel, memory: object | None = None) -> None:
        """Apply ``set_param`` ops reached in order and pop finished goals."""
        while (op := self.current()) is not None:
            if op["op"] == "set_param":
                self.params = apply_set_param(self.floor_params, self.params, op)
                self._pop_current(memory)
                continue
            if not goal_done(op, world, self, memory=memory):
                if op["op"] == "wait" and self.wait_started_tick is None and world.pos is not None:
                    self.wait_started_tick = world.tick
                return
            self._pop_current(memory)

    def drop_current(self, reason: str) -> None:
        op = self.current()
        if op is not None:
            log.warning("plan: dropped op %r: %s", op, reason)
        self._pop_current(None)

    def note_stalled(self, tick: int) -> bool:
        """Record that the current op found no path; True once it has stalled too long."""
        if self.stalled_since_tick is None:
            self.stalled_since_tick = tick
        return tick - self.stalled_since_tick >= PLAN_STALL_SECONDS * self.tick_hz

    def _pop_current(self, memory: object | None = None) -> None:
        op = self.current()
        if op is not None and op["op"] == "fight_boss":
            _clear_boss_memory(memory)
        self.index += 1
        self.wait_started_tick = None
        self.stalled_since_tick = None

    @classmethod
    def from_directives(
        cls,
        *,
        directive_goals: list[str],
        directive_params: dict[str, float | int],
    ) -> Plan | None:
        stack_goals = [g for g in directive_goals if not is_travel_goal(g)]
        if not stack_goals:
            return None
        ops = parse_directives_goals(stack_goals)
        if not ops:
            log.warning("plan: no valid directives goal in %r; using the built-in plan", directive_goals)
            return None
        floor = dict(directive_params)
        return cls(list(ops), dict(floor), floor_params=floor)

    @classmethod
    def from_policy(cls, policy: Policy, directive_params: dict[str, float | int]) -> Plan:
        return cls(list(builtin_goals(policy)), dict(directive_params), floor_params=dict(directive_params))


def builtin_goals(policy: Policy) -> list[GoalOp]:
    """``policy.goals`` as ops, one for one, when directives set no goals and there is no model.

    ``wander`` has no op; it is left to ``policy.goals``, which ``replan``
    falls back to whenever the stack has no path (or is empty).
    """
    ops: list[GoalOp] = []
    for goal in policy.goals:
        if goal == "explore":
            ops.append({"op": "explore_area", "x": 0, "y": 0, "radius": EXPLORE_ANYWHERE})
        elif goal == "doors":
            ops.append({"op": "travel", "to": "entrance", "x": 0, "y": 0})
        elif goal == "goto" and policy.goto is not None:
            x, y = policy.goto
            op: GoalOp = {"op": "travel", "to": "point", "x": x, "y": y}
            if policy.goto_map is not None:
                op["map_id"] = policy.goto_map
            ops.append(op)
        elif goal == "hold":
            ops.append({"op": "wait", "seconds": HOLD_SECONDS})
    return ops


def explore_targets(op: GoalOp, world: WorldModel) -> set[Pos]:
    """Frontier cells inside an ``explore_area`` op, other than where we stand."""
    frontier = world.view.frontier() - {world.pos}
    if op["radius"] >= EXPLORE_ANYWHERE:
        return frontier
    center = (op["x"], op["y"])
    return {p for p in frontier if chebyshev(p, center) <= op["radius"]}


def _fight_boss_done(world: WorldModel, memory: object | None) -> bool:
    if memory is None:
        return False
    engaged = getattr(memory, "boss_engaged", False)
    if not engaged:
        return False
    for ent in world.entities:
        if ent.kind == "npc" and ent.health is not None:
            return ent.health <= 0
    return getattr(memory, "boss_start_health", None) is not None


def _clear_boss_memory(memory: object | None) -> None:
    if memory is None:
        return
    if hasattr(memory, "boss_engaged"):
        memory.boss_engaged = False
        memory.boss_door = None
        memory.boss_start_health = None


def goal_done(op: GoalOp, world: WorldModel, plan: Plan, *, memory: object | None = None) -> bool:
    """Whether ``op`` is finished. Reads only; ``Plan.advance`` pops it."""
    if world.pos is None:
        return False
    name = op["op"]
    if name == "fight_boss":
        return _fight_boss_done(world, memory)
    if name == "wait":
        if plan.wait_started_tick is None:
            return op["seconds"] == 0
        return world.tick - plan.wait_started_tick >= op["seconds"] * plan.tick_hz
    if name == "explore_area":
        if explore_targets(op, world):
            return False
        if op["radius"] >= EXPLORE_ANYWHERE:
            return True
        # A bounded area counts as explored only once we have seen into it.
        center = (op["x"], op["y"])
        return center in world.view.tiles or chebyshev(world.pos, center) <= op["radius"]
    if name == "travel":
        if op["to"] == "point":
            dest_map = op.get("map_id", world.map_id)
            return dest_map == world.map_id and world.pos == (op["x"], op["y"])
        if op["to"] == "entrance":
            return world.view.tiles.get(world.pos) in DOORS
        if op["to"] == "town":
            return (world.map_id, world.pos) in world.respawn_anchors
    # Ops whose states are not shipped never finish here; replan drops them.
    return False


def load_plan_json(text: str, *, floor_params: dict[str, float | int]) -> Plan:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("plan: invalid JSON: %s", e)
        return Plan([], dict(floor_params), floor_params=dict(floor_params))
    goals, params, notes = parse_plan_payload(raw, floor_params=floor_params)
    return Plan(goals, params, notes=notes, floor_params=dict(floor_params))
