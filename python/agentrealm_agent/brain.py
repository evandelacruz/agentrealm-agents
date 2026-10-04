"""Scheduler (choose_call) and the runner's adapter over the state machine (A5).

Tick intents come from ``states.dispatch``; ``decide`` is a shim that maps its
``list[Intent]`` outcome to a one-intent ``Decision`` until the runner sends
whole queues (M6). Both paths are pure in the world model and policy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .config import Policy
from .executor.movement import step_landing
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation import known_prefix
from .navigation.rejection import navigation_avoid_costly
from .pathing import step_open
from .poll_cadence import gate_tick_call, is_urgent
from .states import PlayContext, dispatch
from .states.intents import set_position, take, use_on, withdraw_all
from .world import DOORS, Pos, WorldModel
from .zone_discovery import next_zone_probe

__all__ = [
    "Decision",
    "Memory",
    "choose_call",
    "decide",
    "path_blockers",
    "remaining_path_stale",
    "set_position",
    "take",
    "use_on",
    "walkable_prefix",
    "withdraw_all",
]

SELF_REFRESH = 60  # windows between self reads when nothing forces one


# Scheduler.

def choose_call(w: WorldModel, m: Memory, policy: Policy) -> str:
    """One of: self, position, terrain, entities, zone, tick, skip.

    skip spends nothing this window: calm, and the last poll's queue still
    covers it (poll_cadence, M6). The reads rank above tick, so a calm gap's
    spare windows go to stale terrain first, then stale entities, then a
    pending ``get_zone`` when the gap would otherwise be skipped (A7). Urgent
    windows still take those reads, because entities come only from reads
    until snapshot deltas fold them in (M6 remaining), but never a zone read.
    idle reads nothing past position, zones included: it only polls for events.
    """
    m.zone_probe = None
    if m.cancel_queue:
        return "tick"  # stop the stale queue before reading anything
    if m.need_self or m.windows_since_self >= SELF_REFRESH:
        return "self"
    if m.need_position or w.pos is None:
        return "position"
    if policy.kind in ("idle",):
        return gate_tick_call(w, m, policy)
    if w.terrain_stale():
        return "terrain"
    if m.alarm or w.tick - w.entities_tick >= policy.entity_refresh:
        return "entities"
    return _tick_zone_or_skip(w, m, policy)


def _tick_zone_or_skip(w: WorldModel, m: Memory, policy: Policy) -> str:
    call = gate_tick_call(w, m, policy)
    if call == "skip" and not is_urgent(w, m, policy):
        m.zone_probe = next_zone_probe(w, m)
        if m.zone_probe is not None:
            return "zone"
    return call


@dataclass
class Decision:
    intent: dict | None
    reason: str
    # Reflexes 2–4b: urgent enough to drop a queue still in flight (M6).
    reflex: bool = False


def decide(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    *,
    never_attack: list[str] | None = None,
    knowledge: KnowledgeBase | None = None,
) -> Decision:
    """Run the priority dispatcher (A5) and keep its first intent as a Decision."""
    ctx = PlayContext(m, policy, rng, never_attack=never_attack or [], knowledge=knowledge)
    outcome = dispatch(w, ctx)
    intent = outcome.intents[0] if outcome.intents else None
    return Decision(intent, outcome.reason, outcome.reflex)


def walkable_prefix(
    w: WorldModel, m: Memory, policy: Policy, path: list[Pos], knowledge: KnowledgeBase | None = None
) -> list[Pos]:
    """The path's known prefix, cut before the first cell not open now (A12).

    The cost grid prices occupants instead of refusing them, so a plan may
    run through an NPC; the executor never queues a Step onto one.
    """
    avoid = _plan_avoid(w, m, policy, knowledge)
    out: list[Pos] = []
    for p in known_prefix(path, w.view):
        if _cell_on_path_blocked(w, avoid, p):
            break
        out.append(p)
    return out


def _plan_avoid(
    w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None = None
) -> set[Pos]:
    """Tiles movement plans treat as impassable (hazards, rejections, escape)."""
    nav_avoid, _ = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    if w.pos is not None and w.pos in hazards and not w.open_neighbours(w.pos, blocked):
        return blocked - hazards
    return blocked


def _cell_on_path_blocked(w: WorldModel, avoid: set[Pos], p: Pos) -> bool:
    if p in avoid:
        return True
    if w.view.tiles.get(p) in DOORS:
        return False
    if not w.view.walkable(p):
        return True
    return p in w.occupied()


def _remaining_walk_cells(w: WorldModel, m: Memory) -> list[Pos]:
    """Tiles the held walk queue still steps onto, from tracked position.

    Only the queue on the server counts: the plan past its horizon is
    replanned when the queue runs out anyway.
    """
    if w.pos is None or m.pending_intents is None:
        return []
    pos = w.pos
    cells: list[Pos] = []
    for i in range(m.pending_next_index, len(m.pending_intents)):
        intent = m.pending_intents[i]
        if intent.get("verb") != "Step":
            continue
        pos = step_landing(pos, intent["direction"])
        cells.append(pos)
    return cells


def path_blockers(w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None = None) -> set[Pos]:
    """Cells the rest of the held walk queue steps onto that are not open now."""
    cells = _remaining_walk_cells(w, m)
    if not cells or not w.alive or policy.kind == "idle":
        return set()
    avoid = _plan_avoid(w, m, policy, knowledge)
    out = {p for p in cells[1:] if _cell_on_path_blocked(w, avoid, p)}
    if not step_open(w, avoid, cells[0]):
        out.add(cells[0])
    return out


def remaining_path_stale(w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None = None) -> bool:
    """True when the rest of a held walk queue no longer matches the map (A43).

    A cell that was already blocked when the queue was sent does not count:
    the replan could not avoid it, so resending would only send it again.
    """
    return bool(path_blockers(w, m, policy, knowledge) - m.path_blockers)
