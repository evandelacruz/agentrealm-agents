"""Fight or flee: one decision per engagement, from one estimate (A94).

An engagement starts when a hostile comes within ``policy.hostile_range``
(``survival.combat_group``) and ends once none is in range and none has hit
us within ``survival.THREAT_MEMORY_TICKS``. Its group only grows: a member
that steps out of range is still in the fight.

The decision is the win estimate (``survival.win_ratio``: live health, the
threat table's measured hits and misses, the armed weapon) against a bar:

- ``on_hostile = "fight"``: the effective fight margin (``would_lose``'s).
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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .survival import (
    combat_group,
    effective_fight_margin,
    effective_risk,
    estimate_health,
    recently_attacked,
    win_ratio,
)
from .world import WorldModel

if TYPE_CHECKING:
    from .config import Policy
    from .memory import Memory

HostileKey = tuple[str, int]

# The win ratio at which both sides go down together: above it we kill the
# group before it kills us.
BREAK_EVEN = 1.0


@dataclass(frozen=True)
class Engagement:
    """One fight-or-flee decision: the hostiles it is against, whether
    running away was found not to open distance, the estimate, and whether
    it says fight."""

    group: frozenset[HostileKey]
    cannot_outrun: bool = False
    ratio: float = float("inf")
    fight: bool = False


def sync_engagement(w: WorldModel, m: Memory, policy: Policy, params: dict[str, float | int]) -> Engagement | None:
    """Bring ``m.engagement`` up to this decision; None with no hostile in it.

    Dispatch calls it once per decision, before any state runs, so every
    state reads the same decision (``fights``).

    A hostile that joins the engagement stretches its post's reach to where
    we stand (``WorldModel.note_came_for_us``): it came out that far for us.
    That changes what ground costs, so committed targets are priced again
    (``Memory.reprice_targets``).
    """
    if policy.on_hostile == "ignore" or not w.alive or w.pos is None:
        m.engagement = None
        return None
    joined = combat_group(w, policy)
    e = m.engagement
    if not joined and (e is None or not recently_attacked(w)):
        m.engagement = None
        return None
    known = e.group if e is not None else frozenset()
    newcomers = [h for h in joined if (h.kind, h.id) not in known]
    for h in newcomers:
        w.note_came_for_us(h)
    if newcomers:
        m.reprice_targets()
    group = known | {(h.kind, h.id) for h in joined}
    m.engagement = decide(w, policy, params, group, e is not None and e.cannot_outrun)
    return m.engagement


def decide(
    w: WorldModel, policy: Policy, params: dict[str, float | int], group: frozenset[HostileKey], outrun_failed: bool
) -> Engagement:
    """The estimate against the group's members in view, and the decision it gives."""
    members = [h for h in w.entities if (h.kind, h.id) in group]
    ratio = win_ratio(estimate_health(w), members, w.threat, w.armed_code)
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


def fights(m: Memory) -> bool:
    """This decision's engagement says fight (``sync_engagement``)."""
    return m.engagement is not None and m.engagement.fight
