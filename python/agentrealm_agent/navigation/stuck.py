"""Stuck detection and escalation steps 1, 3 and 5 (A15).

See docs/PLAYABLE_AGENT_PLAN.md Navigation §3–§4. One ``NavAttempt`` per
(goal, map, target) on the current map. Each escalation level gets a fresh
progress window, so a level is left only when its own window fails:

- WALK (0): walk the plan. Stuck after 20 moves or 30 s without the remaining
  path shortening, oscillation, 3 rejected moves in a row, or no path.
- CAUTIOUS (1): step 1, replan with fog priced ``FOG_CAUTIOUS`` and the learned
  blocks; fails like WALK (no path fails it at once).
- REVEAL (3): step 3, up to ``REVEAL_MOVE_BUDGET`` moves over seen ground,
  along the obstacle, toward the reachable frontier nearest the target. Ends
  when a plan shorter than any seen before turns up (then REVEALED), or the
  budget runs out or no frontier is reachable (give up).
- REVEALED (4): walk the plan reveal found; failing again gives up.
- Step 5: give up, back off ``BACKOFF_BASE_TICKS * 2**n`` and queue a strategist
  ``stuck`` signal. Steps 2 and 4 are M9 (A28, A26).

Progress is the remaining length of the planned path plus the straight-line
rest to the target, read when the state plans; a move only bumps counters, so
no search runs per move.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..world import NEIGHBOURS, WALKABLE, Pos, WorldModel, chebyshev
from .planner import CostGridParams

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
REVEAL_SEARCH_NODES = 400  # FINE_NODE_BUDGET: seen ground searched for a frontier

# Escalation step 5: exponential backoff before retrying the goal (§4.5).
BACKOFF_BASE_TICKS = 300

WALK, CAUTIOUS, REVEAL, REVEALED = 0, 1, 3, 4

# Goals that share one key per cell, so a frontier or door dropped under one
# is skipped by the others (Explore's goals and Level's walks).
SHARED_KEYS = {"explore": "frontier", "explore_area": "frontier", "level:frontier": "frontier", "level:door": "doors"}
FRONTIER_GOALS = frozenset(g for g, kind in SHARED_KEYS.items() if kind == "frontier")

ATTEMPTS_KEPT = 32
SIGNALS_KEPT = 16  # nothing drains them until the strategist (A35)


@dataclass
class NavAttempt:
    """Progress toward one navigation target, and how far it has escalated."""

    key: str
    goal: str  # the ``Memory.goal`` label of the path that pursues it
    target: Pos
    map_id: int
    level: int = WALK
    window_tick: int = 0  # tick the window started or last made progress
    best: int | None = None  # shortest remaining measure in this window
    best_ever: int | None = None  # shortest across every level
    moves: int = 0  # applied moves since the window last made progress
    reject_streak: int = 0  # rejected moves since the last applied one
    recent: list[Pos] = field(default_factory=list)
    reveal_left: int = 0
    reasons: list[str] = field(default_factory=list)  # why each level failed, in order
    outline: set[Pos] = field(default_factory=set)
    blocking: set[str] = field(default_factory=set)
    backoff_key: str | None = None  # cross-map legs: ultimate destination's key


@dataclass
class NavStuckMemory:
    attempts: dict[str, NavAttempt] = field(default_factory=dict)
    active: str | None = None
    backoff_until: dict[str, int] = field(default_factory=dict)
    backoff_power: dict[str, int] = field(default_factory=dict)
    stuck_signals: list[dict] = field(default_factory=list)


def goal_key(goal: str, map_id: int | None, target: Pos) -> str:
    kind = SHARED_KEYS.get(goal, goal)
    mid = map_id if map_id is not None else -1
    return f"{kind}:{mid}:{target[0]},{target[1]}"


def is_backed_off(stuck: NavStuckMemory, key: str, tick: int) -> bool:
    return tick < stuck.backoff_until.get(key, -1)


def backed_off(m: Memory, goal: str, map_id: int | None, target: Pos, tick: int) -> bool:
    return is_backed_off(m.nav_stuck, goal_key(goal, map_id, target), tick)


def filter_frontiers(stuck: NavStuckMemory, map_id: int | None, targets: set[Pos], tick: int) -> set[Pos]:
    """``targets`` without the frontier cells given up on and still backed off."""
    if map_id is None:
        return targets
    return {p for p in targets if not is_backed_off(stuck, goal_key("explore", map_id, p), tick)}


def track(
    m: Memory,
    w: WorldModel,
    goal: str,
    target: Pos,
    *,
    backoff_key: str | None = None,
) -> NavAttempt | None:
    """The attempt for ``goal`` at ``target`` on this map, made active.

    An attempt keeps its level when a replan flips to another target and back,
    so alternating between two targets cannot reset either one's escalation.
    """
    if w.map_id is None or w.pos is None:
        return None
    stuck = m.nav_stuck
    key = goal_key(goal, w.map_id, target)
    att = stuck.attempts.get(key)
    if att is None:
        att = stuck.attempts[key] = NavAttempt(key=key, goal=goal, target=target, map_id=w.map_id, window_tick=w.tick)
        while len(stuck.attempts) > ATTEMPTS_KEPT:
            stuck.attempts.pop(next(iter(stuck.attempts)))
    elif stuck.active != key:
        att.goal = goal
        att.window_tick = w.tick  # only time spent pursuing it counts
    att.backoff_key = backoff_key
    stuck.active = key
    return att


def active(m: Memory, w: WorldModel) -> NavAttempt | None:
    """The attempt the agent is pursuing, when it is on this map."""
    stuck = m.nav_stuck
    att = stuck.attempts.get(stuck.active) if stuck.active else None
    if att is None or att.map_id != w.map_id:
        return None
    return att


def finish(m: Memory, att: NavAttempt) -> None:
    """The target was reached or stopped mattering: forget it and its backoff power."""
    stuck = m.nav_stuck
    stuck.attempts.pop(att.key, None)
    stuck.backoff_power.pop(att.key, None)
    if stuck.active == att.key:
        stuck.active = None


def done(att: NavAttempt, w: WorldModel) -> bool:
    """Reached, or a frontier target that is no longer frontier."""
    if w.pos == att.target:
        return True
    return att.goal in FRONTIER_GOALS and att.target not in w.view.frontier()


def planning_params(m: Memory, params: CostGridParams) -> CostGridParams:
    """Step 1 and later plan with fog priced high, so known ground wins."""
    stuck = m.nav_stuck
    att = stuck.attempts.get(stuck.active) if stuck.active else None
    if att is not None and att.level >= CAUTIOUS:
        return dataclasses.replace(
            params,
            avoid=set(params.avoid),
            costly=set(params.costly),
            break_nominated=set(params.break_nominated),
            fog_cost=FOG_CAUTIOUS,
        )
    return params


def measure(path: list[Pos] | None, target: Pos) -> int | None:
    """Remaining moves along ``path`` plus the straight-line rest to ``target``."""
    if not path:
        return None
    return len(path) + chebyshev(path[-1], target)


def observe(att: NavAttempt, w: WorldModel, path: list[Pos] | None) -> None:
    """Progress is the remaining measure falling below this window's best."""
    cost = measure(path, att.target)
    if cost is None:
        return
    if att.best is None or cost < att.best:
        att.best, att.moves, att.window_tick = cost, 0, w.tick
    if att.best_ever is None or cost < att.best_ever:
        att.best_ever = cost


def _oscillating(recent: list[Pos]) -> bool:
    window = recent[-OSCILLATION_WINDOW:]
    if len(window) < OSCILLATION_WINDOW:
        return False
    return len(set(window)) <= OSCILLATION_UNIQUE_MAX


def stuck_reason(att: NavAttempt, tick: int) -> str | None:
    """Why the current window failed, or None. No path is the caller's to report."""
    if att.reject_streak >= REJECT_STREAK_LIMIT:
        return "rejections"
    if att.moves >= PROGRESS_MOVE_LIMIT:
        return "moves"
    if tick - att.window_tick >= PROGRESS_TICK_LIMIT:
        return "time"
    if _oscillating(att.recent):
        return "oscillation"
    return None


def on_step(m: Memory, w: WorldModel) -> None:
    """An applied Step while the active attempt's path is the one being walked."""
    att = active(m, w)
    if att is None or w.pos is None or m.goal != att.goal:
        return
    att.moves += 1
    att.reject_streak = 0
    att.outline.add(w.pos)
    att.recent.append(w.pos)
    del att.recent[: -OSCILLATION_WINDOW * 2]
    if att.level == REVEAL:
        att.reveal_left -= 1


def on_rejection(m: Memory) -> None:
    """A rejected Step of the active attempt's path. Call before the rejection clears the goal."""
    stuck = m.nav_stuck
    att = stuck.attempts.get(stuck.active) if stuck.active else None
    if att is None or m.goal != att.goal:
        return
    att.reject_streak += 1
    if att.level == REVEAL:
        att.reveal_left -= 1  # a reveal that only bumps into walls still ends


def _blocking_types(w: WorldModel, att: NavAttempt) -> set[str]:
    out: set[str] = set()
    for p in att.outline | {att.target} | ({w.pos} if w.pos else set()):
        for dx, dy in NEIGHBOURS:
            block = w.view.tiles.get((p[0] + dx, p[1] + dy))
            if block is not None and block not in WALKABLE:
                out.add(block)
    return out


def _fresh_window(att: NavAttempt, tick: int) -> None:
    att.best, att.moves, att.reject_streak, att.window_tick = None, 0, 0, tick
    att.recent.clear()


def _drop_path(m: Memory, att: NavAttempt) -> None:
    m.path, m.goal = [], ""
    m.corridors.pop(att.goal, None)


def escalate(m: Memory, w: WorldModel, att: NavAttempt, reason: str) -> bool:
    """Leave the failed level for the next one. False when the attempt was given up."""
    att.reasons.append(reason)
    if att.level == WALK:
        att.level = CAUTIOUS
    elif att.level == CAUTIOUS:
        att.level = REVEAL
        att.reveal_left = REVEAL_MOVE_BUDGET
    else:
        give_up(m, w, att)
        return False
    _fresh_window(att, w.tick)
    _drop_path(m, att)
    return True


def reveal_found_way(att: NavAttempt, w: WorldModel, path: list[Pos] | None) -> bool:
    """During reveal: ``path`` is shorter than anything planned before, so walk it (REVEALED)."""
    cost = measure(path, att.target)
    if cost is None or (att.best_ever is not None and cost >= att.best_ever):
        return False
    att.level = REVEALED
    _fresh_window(att, w.tick)
    return True


def give_up(m: Memory, w: WorldModel, att: NavAttempt, reason: str | None = None) -> None:
    """Step 5: back off the goal, drop its path and raise the strategist's ``stuck`` trigger.

    The signal's ``reason`` is why the attempt first got stuck; ``escalation``
    is why each level after it failed.
    """
    if reason is not None:
        att.reasons.append(reason)
    stuck = m.nav_stuck
    backoff = att.backoff_key or att.key
    power = stuck.backoff_power.get(backoff, 0)
    stuck.backoff_until[backoff] = w.tick + BACKOFF_BASE_TICKS * (2**power)
    stuck.backoff_power[backoff] = power + 1
    stuck.stuck_signals.append(
        {
            "trigger": "stuck",
            "reason": att.reasons[0] if att.reasons else "stuck",
            "escalation": list(att.reasons),
            "goal": att.goal,
            "goal_key": att.key,
            "target": list(att.target),
            "map_id": att.map_id,
            "tick": w.tick,
            "outline": [list(p) for p in sorted(att.outline)],
            "blocking": sorted(att.blocking | _blocking_types(w, att)),
        }
    )
    del stuck.stuck_signals[:-SIGNALS_KEPT]
    stuck.attempts.pop(att.key, None)
    if stuck.active == att.key:
        stuck.active = None
    _drop_path(m, att)
    m.goal_op = None


LEVEL_NAMES = {WALK: "", CAUTIOUS: "cautious", REVEAL: "reveal", REVEALED: "revealed"}


def level_note(att: NavAttempt | None) -> str:
    """`` [reveal]`` and the like for a decision's reason, empty at WALK."""
    name = LEVEL_NAMES.get(att.level, "") if att is not None else ""
    return f" [{name}]" if name else ""


def _open(w: WorldModel, blocked: set[Pos], p: Pos) -> bool:
    return p not in blocked and w.view.walkable(p) and p not in w.occupied()


def reveal_step(w: WorldModel, att: NavAttempt, blocked: set[Pos]) -> Pos | None:
    """Step 3: the first move toward the reachable frontier cell nearest the target.

    A breadth-first search over seen, open ground (``REVEAL_SEARCH_NODES``)
    finds the frontier cells we can walk to; the one nearest the target wins,
    nearest us on ties. The route to it runs along the obstacle that stopped
    us. None when the budget is spent or no frontier is reachable, so the
    reveal has nothing left to uncover.
    """
    if att.level != REVEAL or att.reveal_left <= 0 or w.pos is None:
        return None
    start = w.pos
    frontier = w.view.frontier()
    first: dict[Pos, Pos] = {}
    queue = deque([start])
    seen = {start}
    best: Pos | None = None
    best_rank: tuple[int, int] | None = None
    depth = {start: 0}
    while queue and len(seen) <= REVEAL_SEARCH_NODES:
        p = queue.popleft()
        if p != start and p in frontier:
            rank = (chebyshev(p, att.target), depth[p])
            if best_rank is None or rank < best_rank:
                best, best_rank = p, rank
        for dx, dy in NEIGHBOURS:
            n = (p[0] + dx, p[1] + dy)
            if n in seen or not _open(w, blocked, n):
                continue
            seen.add(n)
            depth[n] = depth[p] + 1
            first[n] = n if p == start else first[p]
            queue.append(n)
    return first.get(best) if best is not None else None
