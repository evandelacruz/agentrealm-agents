"""Flee: open distance from hostiles, toward known safety (A9)."""

from __future__ import annotations

from ..pathing import flee_step
from ..survival import hostiles_in_range, on_safe_tile, would_lose
from ..world import Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome
from .boss import boss_fight_on
from .explore import plan_sets
from .fight import can_engage, fight_target
from .intents import set_position


def _toward_safety(w: WorldModel, hostiles: list[Entity], blocked: set[Pos], safes: set[Pos]) -> Pos | None:
    """``flee_step``, breaking ties toward the nearest known safe tile."""
    here = w.pos
    assert here is not None
    options = w.open_neighbours(here, blocked) + [here]

    def score(p: Pos) -> tuple[int, int, Pos]:
        nearest = min(chebyshev(p, h.pos) for h in hostiles)
        to_safe = min(chebyshev(p, s) for s in safes)
        return (nearest, -to_safe, p)

    best = max(options, key=score)
    return None if best == here else best


def should_flee(world: WorldModel, ctx: PlayContext) -> bool:
    """``policy.on_hostile`` as the README documents it.

    ``flee`` flees every hostile in range; ``fight`` flees when there is no
    swingable target (``never_attack``), the win estimate says we lose, or
    the target is out of weapon reach with no open step closer; ``ignore``
    never flees. On a known safe tile, nothing can hurt us, so it stays.
    """
    policy = ctx.policy
    if policy.kind != "scripted" or not world.alive or world.pos is None:
        return False
    if boss_fight_on(world, ctx.memory):
        return False  # Boss retreats out or commits (A38)
    if policy.on_hostile == "ignore" or not hostiles_in_range(world, policy):
        return False
    if on_safe_tile(world):
        return False
    if policy.on_hostile == "flee":
        return True
    target = fight_target(world, policy, ctx.never_attack)
    if target is None or would_lose(world, policy, ctx.params):
        return True
    return not can_engage(world, target, ctx)


class FleeState(State):
    """Priority 2, after **Fight**. Opens distance per ``policy.on_hostile`` when
    hostiles are in range and we are not on a safe tile; stands down during a
    boss fight (A38)."""

    name = "Flee"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return should_flee(world, ctx)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not should_flee(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m, policy = world, ctx.memory, ctx.policy
        hostiles = hostiles_in_range(w, policy)
        if not hostiles or w.pos is None:
            return StateOutcome(None, "no hostiles", state=self.name, wait=True)
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, w.pos), e.id))
        blocked, _, _ = plan_sets(w, m, policy, ctx.knowledge)
        safes = safe_tiles(w, w.map_id) if w.map_id is not None else set()
        away = _toward_safety(w, hostiles, blocked, safes) if safes else flee_step(w, hostiles, blocked)
        if away is None:
            return StateOutcome(None, "nowhere to flee", state=self.name, wait=True)
        m.path = []
        return StateOutcome([set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=self.name)
