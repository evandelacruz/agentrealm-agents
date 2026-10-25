"""Actions for Agent Realm: the ones open whatever the task, and the walk every task uses.

Each action carries its intents (``data["intents"]``, None to send nothing)
and the cell it leaves us on soon (``data["at"]``), which the danger
consideration reads. ``walk`` is the one way any task moves: a path over the
cost grid, sent as a paced Step queue of a few steps, so the next decisions
can change course.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentrealm_adk import supplies
from agentrealm_adk.executor import build_paced_walk_queue
from agentrealm_adk.navigation import CostGridParams, nearest_target
from agentrealm_adk.navigation.rejection import navigation_avoid_costly
from agentrealm_adk.pathing import flee_step
from agentrealm_adk.survival import attack_interval_ticks
from agentrealm_adk.world import Entity, Pos, chebyshev

from adk3.core.interfaces import Action

from .state import State, threat_of

if TYPE_CHECKING:
    from .body import RealmBody

WALK_STEPS = 6  # steps sent per walk; the walk is decided again as they run
NEAR = 6  # cells: a threat this near is worth an action of its own (flee, fight)


def wait() -> Action:
    return Action("wait", ("wait",), "nothing better", {"intents": None})


def walk(s: State, body: RealmBody, targets: set[Pos], key: tuple, why: str, *, door: bool = False) -> Action | None:
    """A walk toward the nearest of ``targets`` by path cost, or None when none is reachable."""
    if not targets:
        return None
    avoid, costly = navigation_avoid_costly(body.m.nav, None, s.map_id, s.tick)
    params = CostGridParams(avoid=avoid, costly=costly, is_hostile=lambda w, e: threat_of(s, e) >= 1.0,
                            allow_goal_door=door)
    found = nearest_target(s.w, set(targets), params)
    if found is None:
        return None
    goal, path = found
    if not path:
        return Action("arrive", key, why, {"intents": None, "at": s.pos, "goal": goal, "path": []})
    return Action("walk", key, why, {"intents": paced(s, body, path[:WALK_STEPS]), "at": path[0],
                                     "goal": goal, "path": path})


def paced(s: State, body: RealmBody, cells: list[Pos]) -> list[dict]:
    last = body.m.last_step_tick
    since = s.tick - last + 1 if last is not None and s.tick - last < 40 else None
    return build_paced_walk_queue(s.pos, cells, movement_speed_milli=s.w.movement_speed, tick_rate_hz=s.tick_hz,
                                  ticks_since_last_step=max(1, since) if since is not None else None)


def common(s: State, body: RealmBody) -> list[Action]:
    """Actions open whatever the task: wait, step away, swing at a threat in reach, drink, pick up what is beside us."""
    if not s.alive:
        return [wait()]
    out = [wait()]
    near = [e for e in s.npcs() if threat_of(s, e) > 0 and chebyshev(e.pos, s.pos) <= NEAR]
    if near:
        avoid, _ = navigation_avoid_costly(body.m.nav, None, s.map_id, s.tick)
        step = flee_step(s.w, near, avoid)
        if step is not None:
            out.append(Action("flee", ("flee",), "step away", {"intents": paced(s, body, [step]), "at": step}))
    out += [a for e in near if (a := swing(s, body, e))]
    if (a := drink(s)) is not None:
        out.append(a)
    out += [take(e) for e in s.supplies() if chebyshev(e.pos, s.pos) <= 1 and not e.gem_price]
    return out


def swing(s: State, body: RealmBody, e: Entity) -> Action | None:
    """A swing at ``e`` when it is in our weapon's reach and the weapon is ready."""
    if not s.armed or not supplies.is_weapon(s.armed):
        return None
    if chebyshev(e.pos, s.pos) > (s.w.attack_range or 1):
        return None
    last = body.last_attack_tick
    if last is not None and s.tick - last < attack_interval_ticks(s.armed) * s.tick_hz // 10:
        return None
    return Action("attack", ("attack", e.id), f"swing at {e.code or 'npc'} {e.id}",
                  {"intents": [{"verb": "Use", "target": {"kind": "npc", "npc_id": e.id}}], "at": s.pos, "npc": e})


def drink(s: State) -> Action | None:
    """Drink the held potion that heals most without waste, when hurt."""
    missing = s.max_health - s.health
    potions = [(i, c) for i, c in s.held if supplies.is_potion(c) and supplies.used_on_self(c)]
    if missing <= 0 or not potions:
        return None
    sid, code = min(potions, key=lambda p: abs(supplies.heal_of(p[1]) - missing))
    intents = [] if s.armed == code else [{"verb": "Arm", "supply_id": sid}]
    intents.append({"verb": "Use", "target": {"kind": "self"}})
    return Action("drink", ("drink", sid), f"drink {code}", {"intents": intents, "at": s.pos,
                                                             "heal": min(missing, supplies.heal_of(code))})


def take(e: Entity) -> Action:
    return Action("take", ("take", e.id), f"take {e.code}", {"intents": [{"verb": "Take", "supply_id": e.id}],
                                                            "at": e.pos, "supply": e})
