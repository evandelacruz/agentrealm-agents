"""Scheduler (choose_call) and the runner's adapter over the state machine (A5).

Tick intents come from ``states.dispatch``; ``decide`` is a shim that maps its
``list[Intent]`` outcome to a one-intent ``Decision`` until the runner sends
whole queues (M6). Both paths are pure in the world model and policy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import Policy
from .directives import Directives, default_directives
from .plan import Plan
from .gem_yield import GemYieldTracker
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation import known_prefix
from .navigation.rejection import navigation_avoid_costly
from .pathing import remaining_walk_cells, step_open
from .poll_cadence import gate_tick_call, is_urgent
from .states import PlayContext, StateOutcome, dispatch
from .states.flee import cornered
from .states.intents import set_position, take, use_on, withdraw_all
from .survival import hostiles_reaching, pursuer_peaks
from .world import DOORS, Entity, Pos, WorldModel
from .zone_discovery import next_zone_probe

__all__ = [
    "Decision",
    "Memory",
    "choose_call",
    "cornered_decision",
    "decide",
    "path_blockers",
    "path_threats",
    "play_context",
    "remaining_path_stale",
    "set_position",
    "take",
    "use_on",
    "walkable_prefix",
    "withdraw_all",
]

SELF_REFRESH = 60  # windows between self reads when nothing forces one
UNPLACED_SELF_REFRESH = 10  # windows between self reads while alive but not on the map (A5)


# Scheduler.

def choose_call(w: WorldModel, m: Memory, policy: Policy) -> str:
    """One of: self, position, terrain, entities, zone, tick, skip.

    skip spends nothing this window: calm, and the last poll's queue still
    covers it (poll_cadence, M6). The reads rank above tick, so a calm gap's
    spare windows go to stale terrain first, then stale entities, then a
    pending ``get_zone`` when the gap would otherwise be skipped (A7). Entities
    are stale ``entity_refresh`` ticks after the last read or delta, and also
    that long after the last real read once we have moved: deltas alone
    missed hostiles a walk ran into (A16 Walk run 4). Urgent
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
        # Asleep is off the map: position cannot answer, and any intent wakes
        # it (GAME_NOTES Sleep). Sync sends the Wait.
        if w.asleep:
            return "tick"
        # Off the map (dead, or not placed yet): position answers 409
        # not_on_map. Poll ticks for Respawned, which re-reads self and
        # position; while self still says alive, re-read it now and then in
        # case placement brings no event (A5, A58 run 9).
        if not w.placed:
            if w.alive and m.windows_since_self >= UNPLACED_SELF_REFRESH:
                return "self"
            return "tick"
        return "position"
    if policy.kind in ("idle",):
        return gate_tick_call(w, m, policy)
    if w.terrain_stale():
        return "terrain"
    if minimap_due(w, m):
        return "minimap"
    if m.alarm or w.tick - w.entities_tick >= policy.entity_refresh or w.entity_read_due(policy.entity_refresh):
        return "entities"
    return _tick_zone_or_skip(w, m, policy)


# Minimap reads spent on one map to learn its level before giving up on it
# (A145): a map is on the minimap once one of its tiles is revealed.
MINIMAP_TRIES = 2


def minimap_due(w: WorldModel, m: Memory) -> bool:
    """A door step landed on a map whose level is unread: only the minimap
    carries it, and it says whether that door was a level entrance (A145)."""
    entry = m.level_entry
    return (
        entry is not None
        and w.map_id == entry[2]
        and w.map_id not in w.map_levels
        and m.minimap_reads.get(w.map_id, 0) < MINIMAP_TRIES
    )


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
    # Pre-built multi-intent queue (A23 **Fight** attack + retreat tail).
    submit_queue: list[dict] | None = None
    # The state that decided, so the runner knows whose intent it submits (A65).
    state: str = ""
    # Why each state above it that ran sent nothing (``StateOutcome.yielded``): the trace keeps them (A120).
    yielded: list[str] = field(default_factory=list)


def decide(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    *,
    never_attack: list[str] | None = None,
    params: dict[str, float | int] | None = None,
    knowledge: KnowledgeBase | None = None,
    directives: Directives | None = None,
    plan: Plan | None = None,
    gem_cuts: GemYieldTracker | None = None,
    probe: bool = False,
) -> Decision:
    """Run the priority dispatcher (A5) and keep its first intent as a Decision.

    ``probe`` runs only the states that can answer with a reflex, for the
    runner's held-queue probe (A64).
    """
    ctx = play_context(
        m,
        policy,
        rng,
        never_attack=never_attack,
        params=params,
        knowledge=knowledge,
        directives=directives,
        plan=plan,
        gem_cuts=gem_cuts,
        probe=probe,
    )
    outcome = dispatch(w, ctx)
    if outcome.state != "Gather" and not (probe and not outcome.state):
        # Gather did not decide: its last word is stale. A probe no state
        # answered leaves the decision that sent the held queue standing.
        m.gather_status = ""
    return _decision(outcome)


def cornered_decision(w: WorldModel, ctx: PlayContext, hostiles: list[Entity]) -> Decision:
    """Flee's move with no Step open (``flee.cornered``), in ``ctx``
    (``play_context``, as ``decide`` builds it): for the runner, when a walk
    under attack has no Step sound from every possible position
    (``held_queue.open_step``, A128). It drinks a heal that is due, else
    swings at a hostile in reach, else asks the planner; it never just stops."""
    return _decision(cornered(w, ctx, hostiles))


def play_context(
    m: Memory,
    policy: Policy,
    rng: random.Random,
    *,
    never_attack: list[str] | None = None,
    params: dict[str, float | int] | None = None,
    knowledge: KnowledgeBase | None = None,
    directives: Directives | None = None,
    plan: Plan | None = None,
    gem_cuts: GemYieldTracker | None = None,
    probe: bool = False,
) -> PlayContext:
    """The ``PlayContext`` a decision runs in: one build for ``decide`` and
    ``cornered_decision``, so the two never drift apart (the plan's tick
    rate paces every ``Use``, A99)."""
    ctx = PlayContext(
        m,
        policy,
        rng,
        never_attack=never_attack or [],
        knowledge=knowledge,
        directives=directives or default_directives(),
        plan=plan,
        gem_cuts=gem_cuts,
        probe=probe,
    )
    if params is not None:
        ctx.params = params
    return ctx


def _decision(outcome: StateOutcome) -> Decision:
    """A state's outcome as the runner's Decision: its first intent, or its paced queue."""
    intents = outcome.intents
    intent = intents[0] if intents else None
    submit_queue = intents if intents and outcome.paced else None
    return Decision(
        intent, outcome.reason, outcome.reflex, submit_queue=submit_queue, state=outcome.state, yielded=list(outcome.yielded)
    )


def walkable_prefix(
    w: WorldModel, m: Memory, policy: Policy, path: list[Pos], knowledge: KnowledgeBase | None = None
) -> list[Pos]:
    """The path's known prefix, cut before the first cell not open now (A12).

    The cost grid prices occupants instead of refusing them, so a plan may
    run through an NPC; the executor never queues a Step onto one.
    """
    avoid = _plan_avoid(w, m, policy, knowledge)
    out: list[Pos] = []
    for p in known_prefix(path, w.view, w.fords):
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
    if not w.walkable(p):
        return True
    return p in w.occupied() or p in w.for_sale()


def path_blockers(w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None = None) -> set[Pos]:
    """Cells the rest of the held walk queue steps onto that are not open now."""
    cells = remaining_walk_cells(w, m)
    if not cells or not w.alive or policy.kind == "idle":
        return set()
    avoid = _plan_avoid(w, m, policy, knowledge)
    out = {p for p in cells[1:] if _cell_on_path_blocked(w, avoid, p)}
    if not step_open(w, avoid, cells[0]):
        out.add(cells[0])
    return out


def path_threats(w: WorldModel, m: Memory, policy: Policy) -> set[tuple[str, int]]:
    """The known hostiles with a cell the rest of the held walk queue steps onto
    in their reach (``survival.hostiles_reaching``), leaving out the fight's
    group and the last hitter (``pursuer_peaks``) and whoever the walk was
    planned without (``Memory.path_skip``: every hostile, for a losing Retreat).

    Those follow the walk anyway, or it went through their reach on purpose,
    so they never make its queue stale.
    """
    cells = remaining_walk_cells(w, m)
    if not cells or not w.alive or policy.kind == "idle":
        return set()
    return hostiles_reaching(w, policy, cells, skip=set(pursuer_peaks(w, policy)) | m.path_skip)


def remaining_path_stale(w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None = None) -> bool:
    """True when the rest of a held walk queue no longer matches the map (A43),
    or now crosses the reach of a known hostile it did not cross when sent
    (A63 run 4).

    A cell that was already blocked, or a hostile already in reach, when the
    queue was sent does not count: the replan could not avoid it, so resending
    would only send it again.
    """
    if path_blockers(w, m, policy, knowledge) - m.path_blockers:
        return True
    return bool(path_threats(w, m, policy) - m.path_threats)
