"""Stuck detection and escalation steps 1, 3 and 5 (A15).

See docs/PLAYABLE_AGENT_PLAN.md Navigation §3–§4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..world import NEIGHBOURS, WALKABLE, Pos, WorldModel, chebyshev
from .planner import CostGridParams, _search

if TYPE_CHECKING:
    from ..memory import Memory

# Stuck when progress stalls (PLAYABLE_AGENT_PLAN §3).
PROGRESS_MOVE_LIMIT = 20
PROGRESS_TICK_LIMIT = 300  # 30 s at 10 ticks/s
REJECT_STREAK_LIMIT = 3
OSCILLATION_WINDOW = 8
OSCILLATION_UNIQUE_MAX = 3

# Escalation step 1: prefer known ground over fog (§4.1).
FOG_CAUTIOUS = 8

# Escalation step 3: bounded reveal along the obstacle (§4.3).
REVEAL_MOVE_BUDGET = 40

# Escalation step 5: exponential backoff before retrying the goal (§4.5).
BACKOFF_BASE_TICKS = 300


@dataclass
class NavAttempt:
    """Progress toward one navigation target."""

    key: str
    target: Pos
    map_id: int
    escalation: int = 0  # 0 normal, 1 cautious replan, 3 reveal, 5 given up
    last_cost: int | None = None
    last_progress_tick: int = -1
    moves_without_progress: int = 0
    reject_streak: int = 0
    recent: list[Pos] = field(default_factory=list)
    reveal_left: int = 0
    reveal_facing: int = 0  # index into NEIGHBOURS
    outline: set[Pos] = field(default_factory=set)
    blocking: set[str] = field(default_factory=set)


@dataclass
class NavStuckMemory:
    attempt: NavAttempt | None = None
    backoff_until: dict[str, int] = field(default_factory=dict)
    backoff_power: dict[str, int] = field(default_factory=dict)
    stuck_signals: list[dict] = field(default_factory=list)
    stuck_signals_seen: set[str] = field(default_factory=set)


def goal_key(goal: str, map_id: int | None, target: Pos) -> str:
    mid = map_id if map_id is not None else -1
    return f"{goal}:{mid}:{target[0]},{target[1]}"


def is_backed_off(stuck: NavStuckMemory, key: str, tick: int) -> bool:
    return tick < stuck.backoff_until.get(key, -1)


def filter_frontiers(stuck: NavStuckMemory, map_id: int | None, targets: set[Pos], tick: int) -> set[Pos]:
    if map_id is None:
        return targets
    return {p for p in targets if not is_backed_off(stuck, goal_key("frontier", map_id, p), tick)}


def track_plan(m: Memory, w: WorldModel, goal: str, target: Pos) -> None:
    """Remember which target the current path pursues."""
    if w.map_id is None or w.pos is None:
        return
    key = goal_key(goal, w.map_id, target)
    att = m.nav_stuck.attempt
    if att is None or att.key != key or att.target != target or att.map_id != w.map_id:
        m.nav_stuck.attempt = NavAttempt(key=key, target=target, map_id=w.map_id, last_progress_tick=w.tick)


def planning_params(m: Memory, params: CostGridParams) -> CostGridParams:
    att = m.nav_stuck.attempt
    if att is not None and att.escalation >= 1:
        return CostGridParams(
            avoid=set(params.avoid),
            costly=set(params.costly),
            break_nominated=set(params.break_nominated),
            hostile_kinds=params.hostile_kinds,
            allow_goal_door=params.allow_goal_door,
            fog_cost=FOG_CAUTIOUS,
        )
    return params


def remaining_cost(w: WorldModel, target: Pos, params: CostGridParams) -> int | None:
    if w.pos is None:
        return None
    found = _search(w, target, params)
    if found.path is None:
        return None
    return found.cost


def _oscillating(recent: list[Pos]) -> bool:
    window = recent[-OSCILLATION_WINDOW:]
    if len(window) < OSCILLATION_WINDOW:
        return False
    return len(set(window)) <= OSCILLATION_UNIQUE_MAX


def _blocking_types(w: WorldModel, target: Pos) -> set[str]:
    if w.pos is None:
        return set()
    out: set[str] = set()
    for p in {w.pos, target}:
        for dx, dy in NEIGHBOURS:
            n = (p[0] + dx, p[1] + dy)
            block = w.view.tiles.get(n)
            if block is not None and block not in WALKABLE:
                out.add(block)
    return out


def stuck_reason(m: Memory, w: WorldModel, target: Pos, params: CostGridParams) -> str | None:
    att = m.nav_stuck.attempt
    if att is None or w.pos is None:
        return None
    cost = remaining_cost(w, target, params)
    if cost is None:
        return "no_path"
    if att.reject_streak >= REJECT_STREAK_LIMIT:
        return "rejections"
    if att.moves_without_progress >= PROGRESS_MOVE_LIMIT:
        return "moves"
    if w.tick - att.last_progress_tick >= PROGRESS_TICK_LIMIT:
        return "time"
    if _oscillating(att.recent):
        return "oscillation"
    return None


def on_step(m: Memory, w: WorldModel, params: CostGridParams) -> None:
    att = m.nav_stuck.attempt
    if att is None or w.pos is None or w.map_id != att.map_id:
        return
    att.outline.add(w.pos)
    att.recent.append(w.pos)
    if len(att.recent) > OSCILLATION_WINDOW * 2:
        att.recent = att.recent[-OSCILLATION_WINDOW * 2 :]
    cost = remaining_cost(w, att.target, params)
    if cost is None:
        att.moves_without_progress += 1
        return
    if att.last_cost is None or cost < att.last_cost:
        att.last_cost = cost
        att.last_progress_tick = w.tick
        att.moves_without_progress = 0
        att.reject_streak = 0
    else:
        att.moves_without_progress += 1
    if att.reveal_left > 0:
        att.reveal_left -= 1


def on_rejection(m: Memory) -> None:
    att = m.nav_stuck.attempt
    if att is None:
        return
    att.reject_streak += 1
    att.moves_without_progress += 1


def _raise_stuck(m: Memory, att: NavAttempt, reason: str) -> None:
    sig = att.key
    if sig in m.nav_stuck.stuck_signals_seen:
        return
    m.nav_stuck.stuck_signals_seen.add(sig)
    m.nav_stuck.stuck_signals.append(
        {
            "trigger": "stuck",
            "reason": reason,
            "goal_key": att.key,
            "target": list(att.target),
            "map_id": att.map_id,
            "outline": [list(p) for p in sorted(att.outline)],
            "blocking": sorted(att.blocking),
        }
    )


def give_up(m: Memory, w: WorldModel, reason: str) -> None:
    att = m.nav_stuck.attempt
    if att is None:
        return
    att.blocking |= _blocking_types(w, att.target)
    att.escalation = 5
    power = m.nav_stuck.backoff_power.get(att.key, 0)
    delay = BACKOFF_BASE_TICKS * (2**power)
    m.nav_stuck.backoff_until[att.key] = w.tick + delay
    m.nav_stuck.backoff_power[att.key] = power + 1
    _raise_stuck(m, att, reason)
    m.nav_stuck.attempt = None
    m.path, m.goal, m.goal_op = [], "", None
    m.corridors.clear()


def escalate(m: Memory, w: WorldModel, reason: str) -> int:
    """Advance escalation when stuck. Returns the new level (1, 3, or 5)."""
    att = m.nav_stuck.attempt
    if att is None:
        return 0
    att.blocking |= _blocking_types(w, att.target)
    if att.escalation < 1:
        att.escalation = 1
        att.moves_without_progress = 0
        att.reject_streak = 0
        att.last_cost = None
        m.path, m.goal = [], ""
        m.corridors.clear()
        return 1
    if att.escalation < 3:
        att.escalation = 3
        att.reveal_left = REVEAL_MOVE_BUDGET
        att.reveal_facing = _facing_toward(w.pos, att.target) if w.pos else 0
        att.moves_without_progress = 0
        att.reject_streak = 0
        m.path, m.goal = [], ""
        return 3
    give_up(m, w, reason)
    return 5


def _facing_toward(here: Pos, goal: Pos) -> int:
    dx = max(-1, min(1, goal[0] - here[0]))
    dy = max(-1, min(1, goal[1] - here[1]))
    if dx == 0 and dy == 0:
        return 0
    for i, (ox, oy) in enumerate(NEIGHBOURS):
        if (ox, oy) == (dx, dy):
            return i
    return 0


def _open(w: WorldModel, blocked: set[Pos], p: Pos) -> bool:
    if p in blocked:
        return False
    return w.view.walkable(p) and p not in w.occupied()


def _frontier_toward_goal(w: WorldModel, goal: Pos, blocked: set[Pos]) -> Pos | None:
    if w.pos is None:
        return None
    best: Pos | None = None
    best_rank: tuple[int, int] | None = None
    for p, block in w.view.tiles.items():
        if block not in WALKABLE:
            continue
        touches_fog = any((p[0] + dx, p[1] + dy) not in w.view.tiles for dx, dy in NEIGHBOURS)
        if not touches_fog:
            continue
        if not _open(w, blocked, p):
            continue
        rank = (chebyshev(p, goal), chebyshev(w.pos, p))
        if best_rank is None or rank < best_rank:
            best, best_rank = p, rank
    return best


def reveal_step(w: WorldModel, m: Memory, blocked: set[Pos]) -> Pos | None:
    """Left-hand rule along the obstacle for escalation step 3."""
    att = m.nav_stuck.attempt
    if att is None or att.escalation != 3 or att.reveal_left <= 0 or w.pos is None:
        return None
    here = w.pos
    toward = _frontier_toward_goal(w, att.target, blocked)
    if toward is not None and toward != here:
        for dx, dy in NEIGHBOURS:
            n = (here[0] + dx, here[1] + dy)
            if n == toward and _open(w, blocked, n):
                att.reveal_facing = _facing_toward(here, toward)
                return n
    n_dirs = len(NEIGHBOURS)
    for turn in range(n_dirs):
        idx = (att.reveal_facing + turn) % n_dirs
        dx, dy = NEIGHBOURS[idx]
        n = (here[0] + dx, here[1] + dy)
        if _open(w, blocked, n):
            att.reveal_facing = idx
            return n
    return None


def maybe_escalate(m: Memory, w: WorldModel, target: Pos, params: CostGridParams) -> int:
    """If stuck on the active attempt, escalate. Returns new escalation level."""
    reason = stuck_reason(m, w, target, params)
    if reason is None:
        return m.nav_stuck.attempt.escalation if m.nav_stuck.attempt else 0
    return escalate(m, w, reason)


def in_reveal(m: Memory) -> bool:
    att = m.nav_stuck.attempt
    return att is not None and att.escalation == 3 and att.reveal_left > 0
