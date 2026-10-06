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
- BREAK (2): step 2, ``Break`` a nominated obstacle on the blocked route (A28).
  Arming is not progress: ``ARM_DECISION_LIMIT`` decisions that arm with no
  break ``Use`` resolving between them fail it (``arm_only``).
- REVEALED (4): walk the plan reveal found; failing again tries step 4.
- ALT_ROUTE (5): step 4, replan through the door graph when enclosed (A28).
- A break that opens the way, or an alt route that finds one, walks again
  (WALK) on probation: if that walk fails before standing at least
  ``RESET_PROGRESS_CELLS`` nearer the target than ever before, the ladder
  carries on from the level that reset it, so it still ends in a give-up.
- Step 5: give up, back off ``BACKOFF_BASE_TICKS * 2**n`` and queue a strategist
  ``stuck`` signal.

Progress is the remaining length of the planned path plus the straight-line
rest to the target, read when the state plans; a move only bumps counters, so
no search runs per move.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from ..world import NEIGHBOURS, WALKABLE, Pos, WorldModel, chebyshev
from . import walk as nav_walk
from .planner import CostGridParams

if TYPE_CHECKING:
    from ..memory import Memory

# Stuck when progress stalls (PLAYABLE_AGENT_PLAN §3).
PROGRESS_MOVE_LIMIT = 20
PROGRESS_TICK_LIMIT = 300  # 30 s at 10 ticks/s
REJECT_STREAK_LIMIT = 3
OSCILLATION_WINDOW = 8
OSCILLATION_UNIQUE_MAX = 3

# Escalation step 2: decisions that arm a tool before the window fails.
# A break arms once; more means the arm is going nowhere.
ARM_DECISION_LIMIT = 3

# Escalation step 1: prefer known ground over fog (§4.1).
FOG_CAUTIOUS = 8

# Escalation step 3: bounded reveal along the obstacle (§4.3).
REVEAL_MOVE_BUDGET = 40
REVEAL_SEARCH_NODES = 400  # FINE_NODE_BUDGET: seen ground searched for a frontier

# A ladder reset (a break opened, an alt route found) is earned only by
# standing this many cells nearer the target than the attempt ever had.
RESET_PROGRESS_CELLS = 5

# Escalation step 5: exponential backoff before retrying the goal (§4.5).
BACKOFF_BASE_TICKS = 300

# A give-up on a hub Travel resolves symbolically (``travel:town``,
# ``travel:shop``) is not for the whole run: it lapses after this long, or
# once the agent stands this far (Chebyshev) from where it gave up, since a
# route from elsewhere may well get there (free-play run 2: one pacing
# give-up on town banned it for the run, so a potion buy was dropped and 17
# gems went unspent).
HUB_GIVE_UP_TICKS = 1200
HUB_GIVE_UP_CELLS = 16
HUB_GOALS = frozenset({"travel:town", "travel:shop"})

WALK, CAUTIOUS, BREAK, REVEAL, REVEALED, ALT_ROUTE = 0, 1, 2, 3, 4, 5

# Goals that share one key per cell, so a frontier or door dropped under one
# is skipped by the others (Explore's goals, Level's walks, Heal's zone walk).
# Every walk to a frontier cell must be registered here as "frontier", or a
# cell one walk gave up on stays open to the others.
SHARED_KEYS = {
    "explore": "frontier",
    "explore_area": "frontier",
    "level:frontier": "frontier",
    "level:door": "doors",
    "heal_explore": "frontier",
}
FRONTIER_GOALS = frozenset(g for g, kind in SHARED_KEYS.items() if kind == "frontier")

ATTEMPTS_KEPT = 32
SIGNALS_KEPT = 16  # newest kept until the strategist drains them (A35)


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
    break_x: int | None = None  # stuck step 2: block under break, if any
    break_y: int | None = None
    break_cap: str | None = None
    arm_decisions: int = 0  # stuck step 2: Break decisions that armed since a break Use last resolved
    # A short walk (``pathing.bounded_step``): the decision it was last
    # pursued on, and the tick it began waiting with no step.
    seen_decision: int | None = None
    waiting_since: int | None = None
    # Real progress, not the plan's: the nearest (Chebyshev) the agent stood
    # to the target, and, while a reset to WALK is on probation, that nearest
    # at the reset and the level that reset it.
    closest: int | None = None
    reset_closest: int | None = None
    reset_level: int | None = None


class Leg(NamedTuple):
    """The cell on this map an attempt tracks. For a leg toward a door to
    another map, ``backoff_key`` names the ultimate destination a give-up backs off."""

    target: Pos
    backoff_key: str | None = None


@dataclass
class NavStuckMemory:
    attempts: dict[str, NavAttempt] = field(default_factory=dict)
    active: str | None = None
    backoff_until: dict[str, int] = field(default_factory=dict)
    backoff_power: dict[str, int] = field(default_factory=dict)
    stuck_signals: list[dict] = field(default_factory=list)
    # Travel destinations given up this run, (map_id, cell) -> tick: their ops
    # leave the stack for good, pinned or not, and are never walked again (A16).
    given_up_travel: dict[tuple[int, Pos], int] = field(default_factory=dict)
    # The hub ones among them (``HUB_GOALS``), (map_id, cell) -> where the
    # agent stood when it gave up (map_id, cell): they lapse
    # (``expire_hub_give_ups``).
    given_up_hubs: dict[tuple[int, Pos], tuple[int | None, Pos | None]] = field(default_factory=dict)
    # The oscillation guard (navigation/oscillation.py): cells stood on at
    # recent decisions, the (walk goal, state) of the move into each, the
    # last decision's move, its events waiting for the trace, and the cells
    # survival-only pacing (Flee, Retreat) must escape from next decision.
    recent_cells: list[Pos] = field(default_factory=list)
    recent_moves: list[tuple[str, str]] = field(default_factory=list)
    last_move: tuple[str, str] = ("", "")
    cells_map: int | None = None
    oscillations: list[dict] = field(default_factory=list)
    escape_from: set[Pos] = field(default_factory=set)
    decision: int = 0  # counts decisions (dispatch), so a short walk knows it was pursued on the last one


def goal_key(goal: str, map_id: int | None, target: Pos) -> str:
    kind = SHARED_KEYS.get(goal, goal)
    mid = map_id if map_id is not None else -1
    return f"{kind}:{mid}:{target[0]},{target[1]}"


def key_dest(key: str) -> tuple[int, Pos]:
    """The map and cell a :func:`goal_key` names."""
    _, mid, cell = key.rsplit(":", 2)
    x, y = cell.split(",")
    return int(mid), (int(x), int(y))


def is_backed_off(stuck: NavStuckMemory, key: str, tick: int) -> bool:
    return tick < stuck.backoff_until.get(key, -1)


def backed_off(m: Memory, goal: str, map_id: int | None, target: Pos, tick: int) -> bool:
    return is_backed_off(m.nav_stuck, goal_key(goal, map_id, target), tick)


def filter_frontiers(stuck: NavStuckMemory, map_id: int | None, targets: set[Pos], tick: int) -> set[Pos]:
    """``targets`` without the frontier cells given up on and still backed off."""
    if map_id is None:
        return targets
    return {p for p in targets if not is_backed_off(stuck, goal_key("explore", map_id, p), tick)}


def track(m: Memory, w: WorldModel, goal: str, target: Pos | Leg) -> NavAttempt | None:
    """The attempt for ``goal`` at ``target`` on this map, made active.

    An attempt keeps its level when a replan flips to another target and back,
    so alternating between two targets cannot reset either one's escalation.
    Switching to an attempt restarts its time window: only time spent
    pursuing it counts. So a walk with no move this decision uses
    ``attempt`` instead, which leaves the active one and both windows alone.
    """
    att = attempt(m, w, goal, target)
    if att is None:
        return None
    if m.nav_stuck.active != att.key:
        att.goal = goal
        att.window_tick = w.tick  # only time spent pursuing it counts
    m.nav_stuck.active = att.key
    return att


def attempt(m: Memory, w: WorldModel, goal: str, target: Pos | Leg) -> NavAttempt | None:
    """The attempt for ``goal`` at ``target`` on this map, made if new, not made active."""
    if w.map_id is None or w.pos is None:
        return None
    target, backoff_key = target if isinstance(target, Leg) else Leg(target)
    stuck = m.nav_stuck
    key = goal_key(goal, w.map_id, target)
    att = stuck.attempts.get(key)
    if att is None:
        att = stuck.attempts[key] = NavAttempt(key=key, goal=goal, target=target, map_id=w.map_id, window_tick=w.tick)
        while len(stuck.attempts) > ATTEMPTS_KEPT:
            stuck.attempts.pop(next(iter(stuck.attempts)))
    att.backoff_key = backoff_key
    return att


def active(m: Memory, w: WorldModel) -> NavAttempt | None:
    """The attempt the agent is pursuing, when it is on this map."""
    stuck = m.nav_stuck
    att = stuck.attempts.get(stuck.active) if stuck.active else None
    if att is None or att.map_id != w.map_id:
        return None
    return att


def leg_toward(
    m: Memory, w: WorldModel, goal: str, dest_map: int | None, dest: Pos, path: list[Pos] | None
) -> Leg | None:
    """What an attempt toward ``dest`` tracks on this map (A15).

    On this map, ``dest`` itself. On another map, the door that ends ``path``
    (the route's first leg); with no route found, the door this destination's
    active attempt was already walking to, so a blocked leg keeps escalating.
    None when no route was ever known: that yields without a backoff.
    """
    if dest_map == w.map_id:
        return Leg(dest)
    ultimate = goal_key(goal, dest_map, dest)
    if path:
        return Leg(path[-1], ultimate)
    att = active(m, w)
    if att is not None and att.goal == goal and att.backoff_key == ultimate:
        return Leg(att.target, ultimate)
    return None


def finish(m: Memory, att: NavAttempt) -> None:
    """The target was reached or stopped mattering: forget it and its backoff power."""
    stuck = m.nav_stuck
    stuck.attempts.pop(att.key, None)
    stuck.backoff_power.pop(att.key, None)
    if stuck.active == att.key:
        stuck.active = None


def finish_in_reach(m: Memory, w: WorldModel, goal: str) -> None:
    """Forget ``goal``'s attempts on this map whose target is in reach: the walk
    is over (a Heal or Loot ``Take``), so a later walk there starts fresh."""
    if w.pos is None:
        return
    for att in list(m.nav_stuck.attempts.values()):
        if att.goal == goal and att.map_id == w.map_id and chebyshev(att.target, w.pos) <= 1:
            finish(m, att)


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
            break_costs=dict(params.break_costs),
            fog_cost=FOG_CAUTIOUS,
        )
    return params


def clear_break_target(att: NavAttempt) -> None:
    att.break_x, att.break_y, att.break_cap = None, None, None


def walk_again(att: NavAttempt, w: WorldModel, from_level: int) -> None:
    """``from_level`` found a way: walk it (WALK), on probation.

    The reset holds only if the walk then stands ``RESET_PROGRESS_CELLS``
    nearer the target than ever; otherwise :func:`escalate` resumes the
    ladder after ``from_level``, so a way that leads nowhere cannot loop.
    """
    _note_position(att, w)
    if att.reset_closest is None:
        att.reset_closest = att.closest
    att.reset_level = max(att.reset_level or WALK, from_level)
    att.level = WALK
    clear_break_target(att)


def on_break_tried(m: Memory, w: WorldModel) -> None:
    """A break ``Use`` resolved without opening the block (it did nothing, or
    was refused): the tool was tried, so arming the next one starts a fresh count."""
    att = active(m, w)
    if att is not None and att.level == BREAK:
        att.arm_decisions = 0


def on_break_opened(m: Memory, w: WorldModel, att: NavAttempt | None) -> None:
    """A break cleared the way: walk the route again, on probation (``walk_again``)."""
    if att is None:
        return
    walk_again(att, w, BREAK)
    _fresh_window(att, w.tick)
    _drop_path(m, att)


def awaiting_break(m: Memory, w: WorldModel, goal: str) -> bool:
    """``goal``'s attempt is at step 2: ``guided_step`` sends nothing and **Break** acts.

    A state walking with ``guided_step`` above Break in dispatch yields the
    round instead of falling back, so dispatch reaches Break (A28).
    """
    att = active(m, w)
    return att is not None and att.goal == goal and att.level == BREAK


def break_target(att: NavAttempt) -> Pos | None:
    if att.break_x is None or att.break_y is None:
        return None
    return att.break_x, att.break_y


def measure(path: list[Pos] | None, target: Pos) -> int | None:
    """Remaining moves along ``path`` plus the straight-line rest to ``target``."""
    if not path:
        return None
    return len(path) + chebyshev(path[-1], target)


def observe(att: NavAttempt, w: WorldModel, path: list[Pos] | None) -> None:
    """Progress is the remaining measure falling below this window's best."""
    _note_position(att, w)
    cost = measure(path, att.target)
    if cost is None:
        return
    if att.best is None or cost < att.best:
        att.best, att.moves, att.window_tick = cost, 0, w.tick
    if att.best_ever is None or cost < att.best_ever:
        att.best_ever = cost


def _note_position(att: NavAttempt, w: WorldModel) -> None:
    if w.pos is None or w.map_id != att.map_id:
        return
    d = chebyshev(w.pos, att.target)
    if att.closest is None or d < att.closest:
        att.closest = d


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
    _note_position(att, w)
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


def resume(m: Memory, att: NavAttempt, tick: int) -> None:
    """A short walk's window and wait hold only while it is pursued on
    consecutive decisions; otherwise both start again now (``pathing.bounded_step``).

    Pursued means tried at all, step or not, so a walk that waits behind an
    occupant keeps its wait, and one Heal left (health back, food eaten
    on the way, a higher state's turn) starts fresh when it comes back.
    """
    now = m.nav_stuck.decision
    if att.seen_decision not in (now, now - 1):
        _fresh_window(att, tick)
        att.waiting_since = None
    att.seen_decision = now


def _fresh_window(att: NavAttempt, tick: int) -> None:
    att.best, att.moves, att.reject_streak, att.window_tick = None, 0, 0, tick
    att.arm_decisions = 0
    att.recent.clear()


def _drop_path(m: Memory, att: NavAttempt) -> None:
    """Drop the path and the walk's commitment (``navigation.walk``), so a dropped route never comes back."""
    m.path, m.goal = [], ""
    nav_walk.drop(m, att.goal)
    m.corridors.pop(att.goal, None)


def escalate(m: Memory, w: WorldModel, att: NavAttempt, reason: str) -> bool:
    """Leave the failed level for the next one. False when the attempt was given up.

    A walk on probation (``walk_again``) that failed without getting nearer
    resumes the ladder after the level that reset it.
    """
    att.reasons.append(reason)
    if att.reset_level is not None:
        earned = (
            att.closest is not None
            and att.reset_closest is not None
            and att.closest <= att.reset_closest - RESET_PROGRESS_CELLS
        )
        if not earned and att.level in (WALK, CAUTIOUS):
            att.level = att.reset_level
        att.reset_closest = att.reset_level = None
    if att.level == WALK:
        att.level = CAUTIOUS
    elif att.level == CAUTIOUS:
        att.level = BREAK
    elif att.level == BREAK:
        att.level = REVEAL
        att.reveal_left = REVEAL_MOVE_BUDGET
        clear_break_target(att)
    elif att.level in (REVEAL, REVEALED):
        att.level = ALT_ROUTE
    elif att.level == ALT_ROUTE:
        give_up(m, w, att)
        return False
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
    if att.goal.startswith("travel:"):
        dest = key_dest(backoff)
        stuck.given_up_travel.setdefault(dest, w.tick)
        if att.goal in HUB_GOALS and stuck.given_up_travel[dest] == w.tick:
            stuck.given_up_hubs[dest] = (w.map_id, w.pos)
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
    nav_walk.drop(m, att.goal, att.target)  # never picked up again, whoever holds the path now
    if m.goal == att.goal:  # another walk's path, such as a waiting goto's, is not this attempt's to drop
        _drop_path(m, att)
        m.goal_op = None
    else:
        m.corridors.pop(att.goal, None)


def hub_give_up_lapses(stuck: NavStuckMemory, dest: tuple[int, Pos]) -> tuple[int, Pos | None] | None:
    """When a hub give-up on ``dest`` lapses: (tick, the cell it gave up
    from), or None for one that holds for the run."""
    if dest not in stuck.given_up_hubs or dest not in stuck.given_up_travel:
        return None
    return stuck.given_up_travel[dest] + HUB_GIVE_UP_TICKS, stuck.given_up_hubs[dest][1]


def expire_hub_give_ups(stuck: NavStuckMemory, w: WorldModel) -> list[tuple[int, Pos]]:
    """Forget the hub give-ups that lapsed, and return them: ``HUB_GIVE_UP_TICKS``
    passed, or the agent stands ``HUB_GIVE_UP_CELLS`` from where it gave up
    (or on another map). Its ops may then be planned and walked again."""
    lapsed = []
    for dest, (mid, cell) in list(stuck.given_up_hubs.items()):
        since = stuck.given_up_travel.get(dest)
        moved = w.pos is not None and (mid != w.map_id or cell is None or chebyshev(cell, w.pos) >= HUB_GIVE_UP_CELLS)
        if since is None or w.tick - since >= HUB_GIVE_UP_TICKS or moved:
            del stuck.given_up_hubs[dest]
            stuck.given_up_travel.pop(dest, None)
            lapsed.append(dest)
    return lapsed


LEVEL_NAMES = {
    WALK: "",
    CAUTIOUS: "cautious",
    BREAK: "break",
    REVEAL: "reveal",
    REVEALED: "revealed",
    ALT_ROUTE: "alt_route",
}


def level_note(att: NavAttempt | None) -> str:
    """`` [reveal]`` and the like for a decision's reason, empty at WALK."""
    name = LEVEL_NAMES.get(att.level, "") if att is not None else ""
    return f" [{name}]" if name else ""


def _open(w: WorldModel, blocked: set[Pos], p: Pos) -> bool:
    return p not in blocked and w.view.walkable(p) and p not in w.occupied() and p not in w.for_sale()


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
