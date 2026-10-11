"""Fight or flee: one decision per engagement, from one estimate (A94).

An engagement starts when a hostile comes within ``policy.hostile_range``
(``survival.combat_group``) and ends once none is in range and none has hit
us within ``survival.THREAT_MEMORY_TICKS``. Its group only grows: a member
that steps out of range is still in the fight.

An engagement whose decision is fight holds until that fight ends (A113):
while Fight still has a target in it (the ``held_target`` dispatch passes:
the very hostile Fight would swing at or step toward, ``fight.held_target``),
it does not end, and Fight goes on with that hostile, so a walking op never
resumes between swings at a hostile that stepped back to its post. It ends
when Fight has no such target (every member dead, gone from view, forbidden
or out of reach), when a chase stops closing the gap
(``CHASE_PROBE_TICKS``, as Flee's probe asks running to work), or when the
decision turns (the Retreat and Flee rules then run as for any engagement).

The decision is the win estimate (``survival.win_ratio``: live health, the
threat table's measured hits and misses, the armed weapon, the armor worn)
against a bar:

- ``on_hostile = "fight"``: the effective fight margin (``fight_margin``
  scaled by risk, ``survival.effective_fight_margin``).
- ``on_hostile = "flee"``: no fight while running works.
- Either, once running away cannot open distance (``cannot_outrun``):
  running only takes free hits, so a fight is picked when it is won
  outright, a ratio above ``BREAK_EVEN``, or the margin when that is lower.

Fight, Flee and Retreat read this one decision, so none of them picks a
fight another would break off. It changes only when the estimate does:
health crosses the bar, a hit or a miss moves a type's odds, a new hostile
joins, or running is found not to work. Where a hostile is, how far, and
whether a step closes on it do not change it; they decide how the decision
is carried out.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from .survival import (
    combat_group,
    effective_fight_margin,
    effective_risk,
    estimate_health,
    recently_attacked,
    win_ratio,
    worn_defense,
)
from .world import Entity, WorldModel, chebyshev

if TYPE_CHECKING:
    from .config import Policy
    from .memory import Memory

HostileKey = tuple[str, int]

# The win ratio at which both sides go down together: above it we kill the
# group before it kills us.
BREAK_EVEN = 1.0

# Ticks a chase after a member out of range gets to bring the gap below the
# nearest it has been; then the member is out of reach and the fight ends.
# The same window Flee's probe gives running away (``flee.FLEE_PROBE_TICKS``).
CHASE_PROBE_TICKS = 30


@dataclass(frozen=True)
class Engagement:
    """One fight-or-flee decision: the hostiles it is against, whether
    running away was found not to open distance, the estimate, whether it
    says fight, and how a chase after a member out of range is going."""

    group: frozenset[HostileKey]
    cannot_outrun: bool = False
    ratio: float = float("inf")
    fight: bool = False
    # While Fight chases a member out of range: the nearest gap so far, and
    # the tick it was reached. None while a member is in range.
    chase: tuple[int, int] | None = None


def sync_engagement(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    params: dict[str, float | int],
    held_target: Callable[[Engagement], Entity | None] | None = None,
) -> Engagement | None:
    """Bring ``m.engagement`` up to this decision; None with no hostile in it.

    Dispatch calls it once per decision, before any state runs, so every
    state reads the same decision (``fights``). With no hostile in range and
    none hitting us, a fight goes on while ``held_target`` names one Fight
    can still act on and the chase after it closes the gap (``chase_on``);
    with no ``held_target``, it ends as a flee does.

    A hostile that joins the engagement off its post stretches that post's
    reach to where we stand (``WorldModel.note_came_for_us``): it came out
    that far for us.
    That changes what ground costs, so committed targets are priced again
    (``Memory.reprice_targets``).
    """
    if policy.on_hostile == "ignore" or not w.alive or w.pos is None:
        m.engagement = None
        return None
    joined = combat_group(w, policy)
    e = m.engagement
    chase = None
    if not joined:
        if e is not None and not recently_attacked(w):
            chase = chase_on(w, e, held_target)
            if chase is None:
                e = None
        if e is None:
            m.engagement = None
            return None
    known = e.group if e is not None else frozenset()
    newcomers = [h for h in joined if (h.kind, h.id) not in known]
    for h in newcomers:
        w.note_came_for_us(h)
    if newcomers:
        m.reprice_targets()
    group = known | {(h.kind, h.id) for h in joined}
    m.engagement = replace(decide(w, policy, params, group, e is not None and e.cannot_outrun), chase=chase)
    return m.engagement


def held_members(w: WorldModel, e: Engagement | None) -> list[Entity]:
    """The members of a fight decided on that are still in view: whom Fight
    may go on with once none is in range (``fight.fight_target``)."""
    if e is None or not e.fight:
        return []
    return [h for h in w.entities if (h.kind, h.id) in e.group]


def chase_on(
    w: WorldModel, e: Engagement, held_target: Callable[[Engagement], Entity | None] | None
) -> tuple[int, int] | None:
    """The fight decided on goes on with no member in range: the chase's
    nearest gap and since when, or None when the fight is over.

    Over when it is not a fight, Fight has no target in it (``held_target``),
    or the gap to that target has not fallen below the nearest it has been
    for more than ``CHASE_PROBE_TICKS``.
    """
    target = held_target(e) if e.fight and held_target is not None and w.pos is not None else None
    if target is None:
        return None
    gap = chebyshev(w.pos, target.pos)
    if e.chase is None or gap < e.chase[0]:
        return gap, w.tick
    if w.tick - e.chase[1] > CHASE_PROBE_TICKS:
        return None
    return e.chase


def decide(
    w: WorldModel,
    policy: Policy,
    params: dict[str, float | int],
    group: frozenset[HostileKey],
    outrun_failed: bool,
    health: int | None = None,
) -> Engagement:
    """The estimate against the group's members in view, and the decision it
    gives; at ``health`` instead of live health when given."""
    members = [h for h in w.entities if (h.kind, h.id) in group]
    ratio = win_ratio(
        estimate_health(w) if health is None else health, members, w.threat, w.armed_code, worn_defense(w)
    )
    bar = fight_bar(w, policy, params, outrun_failed)
    return Engagement(group, outrun_failed, ratio, bool(members) and ratio > bar)


def fight_bar(w: WorldModel, policy: Policy, params: dict[str, float | int], outrun_failed: bool) -> float:
    """The ratio a fight must beat (see the module docstring)."""
    eff = effective_risk(float(params["risk"]), w.lives, int(params["lives_floor"]))
    margin = effective_fight_margin(float(params["fight_margin"]), eff)
    if outrun_failed:
        return min(margin, BREAK_EVEN)
    return margin if policy.on_hostile == "fight" else float("inf")


def cannot_outrun(w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int]) -> Engagement | None:
    """Running away was found not to open distance: decide again on that.

    Called by the state that measured it (Flee's probe, Retreat losing
    ground), in its act, so the new decision is what it carries out."""
    e = m.engagement
    if e is None or e.cannot_outrun:
        return e
    m.engagement = decide(w, policy, params, e.group, True)
    return m.engagement


def drink_turns_fight(w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int], heal: int) -> bool:
    """A drink that heals ``heal`` changes this engagement (A96): the
    estimate at the healed health says fight where the live one does not, or
    wins (above ``BREAK_EVEN``) where the live one loses. False with no
    engagement, or none of its hostiles in view."""
    e = m.engagement
    if e is None:
        return False
    health = estimate_health(w)
    if w.max_health is not None:
        healed = min(w.max_health, health + heal)
    else:
        healed = health + heal
    if healed <= health:
        return False
    now = decide(w, policy, params, e.group, e.cannot_outrun)
    after = decide(w, policy, params, e.group, e.cannot_outrun, health=healed)
    return (after.fight and not now.fight) or (now.ratio <= BREAK_EVEN < after.ratio)


def would_fight(w: WorldModel, policy: Policy, params: dict[str, float | int], also: Entity | None = None) -> bool:
    """The decision an engagement would start with against the hostiles in
    range, and ``also``, one further off a state would go for."""
    group = frozenset((h.kind, h.id) for h in combat_group(w, policy, also))
    return decide(w, policy, params, group, False).fight


def fights(m: Memory) -> bool:
    """This decision's engagement says fight (``sync_engagement``)."""
    return m.engagement is not None and m.engagement.fight
