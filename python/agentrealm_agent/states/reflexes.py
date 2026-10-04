"""Hazard and hostile reflexes every scripted moving state runs first (A5, A22)."""

from __future__ import annotations

from ..config import Policy
from ..directives import attack_forbidden
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import flee_step
from ..world import Pos, WorldModel, chebyshev
from .base import StateOutcome
from .intents import set_position, use_on


def safety_reflex(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    never_attack: list[str],
    knowledge: KnowledgeBase | None,
    state: str,
) -> tuple[StateOutcome | None, set[Pos], set[Pos]]:
    """Step off a hazard, or fight or flee a hostile; else the planning sets.

    Returns the reflex outcome (or None) with the cells planning must avoid
    and the cells it may cross at a cost. A reflex that moves clears
    ``m.path``. Rejection learnings stay out of every choice (A14, reflex 1).
    """
    here = w.pos
    assert here is not None
    view = w.view
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    escape: set[Pos] = set()
    if here in hazards:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path = []
            p = min(safe)
            out = StateOutcome([set_position(p)], f"off {view.tiles.get(here)}", reflex=True, state=state)
            return out, blocked, nav_costly
        escape = hazards
    plan_avoid = blocked - escape
    plan_costly = escape | nav_costly

    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight":
            if target.kind == "character" and not attack_forbidden(target, never_attack):
                out = StateOutcome([use_on(target)], f"fight {target.kind} {target.id}", reflex=True, state=state)
                return out, plan_avoid, plan_costly
        away = flee_step(w, hostiles, blocked)
        if away is not None:
            m.path = []
            out = StateOutcome([set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=state)
            return out, plan_avoid, plan_costly

    return None, plan_avoid, plan_costly
