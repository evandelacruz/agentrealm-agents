"""Plan schema, goal stack, and built-in planning without a model (A34)."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Policy
from .directives import PARAM_DEFAULTS, _valid_param
from .healing import supply_matches
from .fragments import holds_whole
from .executor.constants import DEFAULT_TICK_RATE_HZ
from .memory import Memory, note_goal_done, note_goal_failed
from .travel.ops import parse_travel_string, travel_op_from_plan_goal
from .world import DOORS, Pos, WorldModel, chebyshev

log = logging.getLogger(__name__)

GoalOp = dict[str, Any]

TRAVEL_TO = frozenset({"entrance", "town", "hunting_ground", "shop", "point"})
# Destinations the agent finds itself: they take no x, y (``shop`` and
# ``entrance`` may name one by x, y; without, the nearest known one).
TRAVEL_SYMBOLIC = frozenset({"entrance", "town", "hunting_ground", "shop"})
CAPABILITIES = frozenset({"cut", "chop", "smash", "burn", "blast"})
ALL_PARAMS = frozenset(PARAM_DEFAULTS)

# Op name -> the executor state that carries it out (PLAN.md **Architecture:
# AI plans, state machine executes**). An executor runs only while its op is
# on top of the stack. ``None``: no executor; ``set_param`` is applied by
# ``Plan.advance``; ``hunt`` and ``avoid`` are dropped when they reach the top.
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
    "equip": "Equip",
    "hunt": None,
    "enter_level": "Level",
    "fight_boss": "Boss",
    "avoid": None,
    "wait": "Wait",
    "set_param": None,
}

# The op table: the contract with the planner (A35), shown to the model as is.
# It lists exactly the ops a state executes (OP_STATE), plus ``set_param``,
# which ``Plan.advance`` applies; a test keeps the two in step. A behavior the
# planner needs and the states lack becomes a new op here (with its validator
# and the state that runs it), never a state that starts itself.
OP_FIELDS: dict[str, str] = {
    "travel": 'to ("entrance"|"town"|"hunting_ground"|"shop"|"point"), x, y, optional map_id. "point" needs x, y; "town" and "hunting_ground" take none (the agent finds them; with no hunting ground known it explores and reads zones until it finds one); "shop" and "entrance" take x, y for a given one, else the nearest known',
    "explore_area": "x, y, radius",
    "read": "x, y, or supply_id",
    "say": "text, and exactly one of npc_id (an id from State nearby_npcs) or npc_type (an NPC type code; the nearest NPC of that type in sight)",
    "buy": "code (a potion, a tool, gear). Buys one more, whatever is already held: it is done once one more is held than the fewest held since it reached the top (one picked up for free counts too), so put two buy ops on the stack to buy two. With none in sight it walks to the nearest known shop, or to town to look for one; it is dropped when no known shop sells it or it costs more gems than are held",
    "break_block": 'x, y, capability ("cut"|"chop"|"smash"|"burn"|"blast")',
    "use_block": "x, y, code (the supply to use on it)",
    "compose": "composes_into (the whole item to make)",
    "fetch_item": "code, optional x, y",
    "gather_gems": "count (the gem total to reach), optional x, y (a block of the region to gather in: Gather walks there and cuts only there while it has a cell to cut; it lifts that region's barren mark. The region is committed: a reply that moves it keeps the old one unless that region is barren or poor, Gather went 30 s without getting nearer it or, once there, without a cut there taking effect, or a death, new map, hurt or hostile pack came up)",
    "equip": "optional code (else the best held gear is armed and worn)",
    "enter_level": "x, y (the level door)",
    "fight_boss": "x, y (the boss door), optional min_health, min_potions, armed, worn (list)",
    "wait": "seconds, why",
    "set_param": "name, value",
}

# Ops done once their target block changes from what it was when the op reached the top.
BLOCK_CHANGE_OPS = frozenset({"use_block", "break_block"})
SOLVE_OPS = frozenset({"compose", "use_block"})

# Built-in `explore` explores the whole map: no center, no radius bound.
EXPLORE_ANYWHERE = 1 << 30
# A planner `wait` is short and says why: the agent never idles on a long hold.
MAX_WAIT_SECONDS = 30
# An op whose executor makes no progress for this long is dropped and logged.
PLAN_STALL_SECONDS = 30

# What each survival param means to the planner, and which way tightens it
# (A35). The prompt shows these, and a rejected change repeats its line.
PARAM_MEANINGS: dict[str, str] = {
    "retreat_hits": "how many hits of health Retreat keeps in reserve: it leaves for a safe tile once health is at or below retreat_hits times the hit size of what is attacking. Higher retreats sooner, at more health; lower stays in the fight longer. You may only raise it",
    "fight_margin": "how much the win estimate must favour us before Fight engages. Higher fights fewer, safer fights. You may only raise it",
    "risk": "0..1, how much risk the character takes; it falls toward 0 as lives near lives_floor. Lower is more careful. You may only lower it",
    "lives_floor": "lives at which risk reaches 0. Higher is more careful. You may only raise it",
    "potion_reserve": "potions to keep; buy more below it. You may only raise it",
    "curiosity": "0..1, how far to stray to look at new things. Any value in range",
}

# Shorthand in directives `goals = ["gather_gems:20", "buy:torch"]`.
_SHORTHAND = re.compile(r"^([a-z_]+):(.+)$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


# While :func:`collect_rejections` runs, every dropped op and ignored param
# is also appended here, so the strategist can tell the planner (A35).
_rejections: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("plan_rejections", default=None)


def _reject(message: str) -> None:
    log.warning("plan: %s", message)
    sink = _rejections.get()
    if sink is not None:
        sink.append(message)


def _drop(reason: str, op: object) -> None:
    _reject(f"dropped op {op!r}: {reason}")


def collect_rejections(parse: Callable[[], Any]) -> tuple[Any, list[str]]:
    """Run ``parse`` and return its result with every reason an op or a param
    was dropped meanwhile, for the planner's next State."""
    sink: list[str] = []
    token = _rejections.set(sink)
    try:
        return parse(), sink
    finally:
        _rejections.reset(token)


def _require_fields(op: dict[str, Any], fields: tuple[str, ...]) -> bool:
    for key in fields:
        if key not in op:
            _drop(f"missing `{key}`", op)
            return False
    return True


def _validate_travel(op: dict[str, Any]) -> bool:
    """A ``point`` needs x, y. A symbolic ``to`` may leave them out: they
    become ``0, 0``, which every reader takes as "the agent finds it"."""
    if not _require_fields(op, ("to",)):
        return False
    if not _is_str(op["to"]) or op["to"] not in TRAVEL_TO:
        _drop("bad `to`", op)
        return False
    if op["to"] in TRAVEL_SYMBOLIC and "x" not in op and "y" not in op:
        op["x"] = op["y"] = 0
    if not _require_fields(op, ("x", "y")):
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
    if not (_require_fields(op, ("count",)) and _is_int(op["count"]) and op["count"] >= 0):
        return False
    if "x" in op or "y" in op:
        if not _is_int(op.get("x")) or not _is_int(op.get("y")):
            _drop("bad gather_gems coordinates", op)
            return False
    return True


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
    if not _require_fields(op, ("seconds", "why")):
        return False
    if not _is_str(op["why"]):
        _drop("wait needs a reason", op)
        return False
    if not _is_int(op["seconds"]) or not 0 <= op["seconds"] <= MAX_WAIT_SECONDS:
        _drop(f"wait seconds must be 0..{MAX_WAIT_SECONDS}", op)
        return False
    return True


def _validate_equip(op: dict[str, Any]) -> bool:
    if "code" in op and not _is_str(op["code"]):
        _drop("bad equip code", op)
        return False
    return True


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
    "equip": _validate_equip,
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


def parse_directives_goal(text: str) -> GoalOp | None:
    """Turn one directives shorthand string into a validated op.

    ``travel:<to>[:map_id]:x:y`` (A27), ``gather_gems:N`` and ``buy:code``.
    A ``travel`` with no coordinates (``travel:town``, ``travel:shop``) gets
    ``x = y = 0``: the nearest one the knowledge base knows.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("travel:"):
        t = parse_travel_string(text)
        if t is None:
            _drop("bad travel shorthand", text)
            return None
        op: GoalOp = {"op": "travel", "to": t.to, "x": t.x or 0, "y": t.y or 0}
        if t.map_id is not None:
            op["map_id"] = t.map_id
        return validate_goal_op(op)
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


def parse_plan_payload(
    raw: object,
    *,
    floor_params: dict[str, float | int],
    current_params: dict[str, float | int] | None = None,
) -> tuple[list[GoalOp], dict[str, float | int], str]:
    """Parse strategist JSON: goals, params, notes. Drops invalid ops and params.

    Incoming ``params`` merge onto ``current_params`` (the floor when not given),
    so a reply naming one key leaves the others as they were. Each key is still
    bounded by ``floor_params``.
    """
    current = dict(floor_params if current_params is None else current_params)
    if not isinstance(raw, dict):
        _reject("payload is not an object")
        return [], current, ""
    extra = set(raw) - {"goals", "params", "notes"}
    if extra:
        _reject(f"dropped unknown top-level keys {sorted(extra)}")
    goals_raw = raw.get("goals", [])
    if not isinstance(goals_raw, list):
        _reject("`goals` must be a list")
        goals_raw = []
    goals: list[GoalOp] = []
    for item in goals_raw:
        op = validate_goal_op(item)
        if op is not None and op["op"] == "set_param":
            loosens = loosening(floor_params, op["name"], _valid_param(op["name"], op["value"]))
            if loosens:
                _drop(loosens, op)
                continue
        if op is not None:
            goals.append(op)
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        _reject("`notes` must be a string")
        notes = ""
    params = current
    incoming = raw.get("params")
    if incoming is not None:
        if not isinstance(incoming, dict):
            _reject("`params` must be an object")
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
            _reject(f"unknown param `{name}` ignored")
            continue
        ok = _valid_param(name, value)
        if ok is None:
            _reject(f"param `{name}`={value!r} out of range; ignored")
            continue
        loosens = loosening(floor, name, ok)
        if loosens:
            _reject(loosens)
            continue
        out[name] = ok
    return out


def loosening(floor: dict[str, float | int], name: str, value: float | int) -> str:
    """Why a valid ``value`` for survival param ``name`` loosens it past
    ``floor``, or "" when it does not (``curiosity`` never does)."""
    if name == "curiosity":
        return ""
    floor_val = floor.get(name, PARAM_DEFAULTS[name])
    if name == "risk":
        # risk may only fall; the others may only rise.
        if value <= floor_val:
            return ""
        side = "above"
    elif value >= floor_val:
        return ""
    else:
        side = "below"
    return f"{name} {value} {side} floor {floor_val}, ignored. {name} is {PARAM_MEANINGS[name]}"


def apply_set_param(
    floor: dict[str, float | int], current: dict[str, float | int], op: GoalOp
) -> dict[str, float | int]:
    return apply_strategist_params(floor, current, {op["name"]: op["value"]})


@dataclass
class Plan:
    """Validated goal stack plus effective params (A34).

    ``params`` holds the effective survival params the states read (A9): the
    directives' values, tightened by ``set_param`` or the strategist (A35). ``current`` and ``goal_done`` only read;
    ``advance``, ``drop_current`` and ``finish_current`` are the only calls that move the stack.
    """

    goals: list[GoalOp]
    params: dict[str, float | int]
    notes: str = ""
    index: int = 0
    floor_params: dict[str, float | int] = field(default_factory=lambda: dict(PARAM_DEFAULTS))
    wait_started_tick: int | None = None
    stalled_since_tick: int | None = None  # first tick the current op found no path
    acted: GoalOp | None = None  # the head op the current decision acted on; the runner clears it each round (A36)
    block_before: str | None = None  # block_type at a `use_block` or `break_block` target when first seen as the head op
    held_before: int | None = None  # fewest of a `buy` op's item held or stowed since it became the head op
    tick_hz: int = DEFAULT_TICK_RATE_HZ  # world tick rate; converts `wait` seconds to ticks
    directive_end: int = 0  # goals[index:directive_end] are directives ops, which stay above the planner's

    def snapshot(self) -> tuple:
        """The stack's progress, for :meth:`restore` after a side-effect-free probe."""
        return (
            self.index,
            list(self.goals),
            dict(self.params),
            dict(self.floor_params),
            self.wait_started_tick,
            self.stalled_since_tick,
            self.block_before,
            self.held_before,
            self.directive_end,
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
            self.block_before,
            self.held_before,
            self.directive_end,
        ) = saved
        self.goals, self.params, self.floor_params = list(goals), dict(params), dict(floor_params)

    def current(self) -> GoalOp | None:
        """The op at the top of the stack, or None when it is empty."""
        return self.goals[self.index] if self.index < len(self.goals) else None

    def directive_ops(self) -> list[GoalOp]:
        """The directives ops still left: the planner's goals go below these (A35)."""
        return self.goals[self.index : self.directive_end]

    def advance(self, world: WorldModel, memory: Memory | None = None) -> None:
        """Apply ``set_param`` ops reached in order and pop finished goals."""
        while (op := self.current()) is not None:
            if op["op"] == "set_param":
                self.params = apply_set_param(self.floor_params, self.params, op)
                self._pop_current()
                continue
            if op["op"] in BLOCK_CHANGE_OPS and self.block_before is None and world.map_id is not None:
                self.block_before = world.view.tiles.get((op["x"], op["y"]))
            if op["op"] == "buy":
                # A drink, a drop or a death lowers the bar: the buy still adds one.
                held = count_held(world, op["code"])
                self.held_before = held if self.held_before is None else min(self.held_before, held)
            if not goal_done(op, world, self):
                if op["op"] == "wait" and self.wait_started_tick is None and world.pos is not None:
                    self.wait_started_tick = world.tick
                return
            note_goal_done(memory, op, "goal_done")
            self._pop_current()

    def drop_current(self, reason: str, memory: Memory | None = None) -> None:
        op = self.current()
        if op is not None:
            log.warning("plan: dropped op %r: %s", op, reason)
            note_goal_failed(memory, op, reason)
        self._pop_current()

    def drop_ops(self, match: Callable[[GoalOp], bool], reason: str, memory: Memory | None = None) -> None:
        """Remove every op left on the stack that ``match`` picks, wherever it
        sits, directives ops included (A16): each raises ``goal_failed``."""
        left = self.goals[self.index :]
        if not any(match(op) for op in left):
            return
        head_dropped = match(left[0])
        kept_directives = 0
        kept: list[GoalOp] = []
        for i, op in enumerate(left, self.index):
            if match(op):
                log.warning("plan: dropped op %r: %s", op, reason)
                note_goal_failed(memory, op, reason)
                continue
            kept.append(op)
            kept_directives += i < self.directive_end
        self.goals = self.goals[: self.index] + kept
        self.directive_end = self.index + kept_directives
        if head_dropped:
            self.wait_started_tick = self.stalled_since_tick = self.block_before = self.held_before = None

    def finish_current(self, reason: str, memory: Memory | None = None) -> None:
        """Pop an op whose state saw it finish (``fight_boss``, A38)."""
        op = self.current()
        if op is not None:
            log.info("plan: finished op %r: %s", op, reason)
            note_goal_done(memory, op, reason)
        self._pop_current()

    def note_progress(self) -> None:
        """The state that owns the head op acted on it this decision: a step
        toward it, a Take or a Use for it. Resets the stall clock (A34) and
        records the op in ``acted`` (A36)."""
        self.stalled_since_tick = None
        self.acted = self.current()

    def note_stalled(self, tick: int) -> bool:
        """Record that the current op found no path; True once it has stalled too long."""
        if self.stalled_since_tick is None:
            self.stalled_since_tick = tick
        return tick - self.stalled_since_tick >= PLAN_STALL_SECONDS * self.tick_hz

    def _pop_current(self) -> None:
        self.index += 1
        self.wait_started_tick = None
        self.stalled_since_tick = None
        self.block_before = None
        self.held_before = None

    @classmethod
    def from_directives(
        cls,
        *,
        directive_goals: list[str],
        directive_params: dict[str, float | int],
    ) -> Plan | None:
        if not directive_goals:
            return None
        ops = directive_stack_ops(directive_goals)
        if not ops:
            log.warning("plan: no valid directives goal in %r; using the built-in plan", directive_goals)
            return None
        floor = dict(directive_params)
        return cls(list(ops), dict(floor), floor_params=floor, directive_end=len(ops))

    @classmethod
    def from_policy(
        cls, policy: Policy, directive_params: dict[str, float | int], *, goto_satisfied: bool = False
    ) -> Plan:
        ops = builtin_goals(policy, goto_satisfied=goto_satisfied)
        return cls(list(ops), dict(directive_params), floor_params=dict(directive_params))


def directive_stack_ops(directive_goals: list[str]) -> list[GoalOp]:
    """The stack ops directives ``goals`` set: manual steering, which outranks the planner.

    They sit on top of the stack until each is done or dropped; the planner's
    goals go below them, and own the stack once they are gone (A35).
    """
    return parse_directives_goals(directive_goals)


def builtin_goals(policy: Policy, *, goto_satisfied: bool = False) -> list[GoalOp]:
    """``policy.goals`` as ops, one for one, when directives set no goals and the planner is off.

    That is the ``--no-planner`` test mode only: with the planner on, the
    stack starts empty and only the planner or directives fill it (A35).

    This is the built-in planner; states never read ``policy.goals`` (PLAN.md
    **Architecture**). A ``goto`` the agent already stood on
    (``goto_satisfied``) has no op, so a rebuilt plan never walks back to it (A16).
    """
    ops: list[GoalOp] = []
    for goal in policy.goals:
        if goal == "explore":
            ops.append({"op": "explore_area", "x": 0, "y": 0, "radius": EXPLORE_ANYWHERE})
        elif goal == "doors":
            ops.append({"op": "travel", "to": "entrance", "x": 0, "y": 0})
        elif goal == "goto" and policy.goto is not None and not goto_satisfied:
            x, y = policy.goto
            op: GoalOp = {"op": "travel", "to": "point", "x": x, "y": y}
            if policy.goto_map is not None:
                op["map_id"] = policy.goto_map
            ops.append(op)
    return ops


def explore_targets(op: GoalOp, world: WorldModel) -> set[Pos]:
    """Frontier cells inside an ``explore_area`` op, other than where we stand."""
    frontier = world.view.frontier() - {world.pos}
    if op["radius"] >= EXPLORE_ANYWHERE:
        return frontier
    center = (op["x"], op["y"])
    return {p for p in frontier if chebyshev(p, center) <= op["radius"]}


def goal_done(op: GoalOp, world: WorldModel, plan: Plan) -> bool:
    """Whether ``op`` is finished. Reads only; ``Plan.advance`` pops it."""
    if world.pos is None:
        return False
    name = op["op"]
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
        if op["to"] == "shop":
            here = world.pos
            if here is None:
                return False
            t = travel_op_from_plan_goal(op)
            if t.x is not None and t.y is not None:
                dest_map = t.map_id if t.map_id is not None else world.map_id
                return dest_map == world.map_id and here == (t.x, t.y)
            # Any shop: a priced supply underfoot is one. A bought-out known
            # cell is arrival too; Travel pops that one, it needs the KB.
            return any(
                e.kind == "supply"
                and isinstance(e.gem_price, int)
                and e.gem_price > 0
                and e.pos == here
                for e in world.entities
            )
    if name == "compose":
        return holds_whole(world.held_supplies, op["composes_into"])
    if name in BLOCK_CHANGE_OPS:
        # Done once the target's block_type differs from what it was when the
        # op reached the top: a successful Use destroys the block, which then
        # shows its destroyed type (GAME_NOTES Breaking blocks). BlockChanged
        # events and terrain reads both update the tile, so a missed window
        # does not lose the change.
        tile = world.view.tiles.get((op["x"], op["y"]))
        return plan.block_before is not None and tile is not None and tile != plan.block_before
    if name == "buy":
        # One more than the fewest held since it reached the top, whatever was
        # held before (free-play run 5). One more is what the buy is for, so one
        # picked up or withdrawn for free counts too, and saves the gems.
        return plan.held_before is not None and count_held(world, op["code"]) > plan.held_before
    if name == "fetch_item":
        return any(supply_matches(op["code"], s.code) for s in world.held_supplies)
    if name == "gather_gems":
        return world.gems is not None and world.gems >= op["count"]
    # Their executors finish the rest: Travel on arrival (A27), Boss on the
    # defeat (A38), Investigate once read or greeted (A30), Equip and Level
    # when nothing is left to do.
    return False


def count_held(world: WorldModel, code: str) -> int:
    """How many supplies held or stowed satisfy a want for ``code`` (``supply_matches``)."""
    return sum(1 for s in world.held_supplies + world.chest_supplies if supply_matches(code, s.code))


def load_plan_json(text: str, *, floor_params: dict[str, float | int]) -> Plan:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("plan: invalid JSON: %s", e)
        return Plan([], dict(floor_params), floor_params=dict(floor_params))
    goals, params, notes = parse_plan_payload(raw, floor_params=floor_params)
    return Plan(goals, params, notes=notes, floor_params=dict(floor_params))
