"""Greet: say hello once to a nearby helper not yet spoken to (reflex, A65).

The cheap, deterministic half of talking to NPCs, in the spirit of Pickup:
it acts on what is in sight now and never walks. The API does not say which
NPCs are helpers, so Greet takes an NPC for one only when it stays put and
has not hit us, and greets none off safe ground while ``policy.hostile``
names NPCs. ``Say`` reaches
``SPEECH_RANGE`` blocks, further than sight, so every NPC in sight is in
reach. Walking to an NPC is **Investigate**'s, for a planner ``say`` op.

A helper's reply arrives as a ``SpokenTo`` event, which ``clues.py`` stores
as a clue for the planner. An applied hello lands the NPC in the knowledge
base's ``greeted_npcs`` (the runner records it), so each NPC is greeted once.
That record is Greet's own: ``spoken_npcs`` and the ``say`` op's refusal
count belong to the planner's ``say`` ops, so a hello never settles one.
"""

from __future__ import annotations

from ..executor.pacing import SPEECH_INTERVAL_TICKS
from ..investigation import (
    GREET_TEXT,
    HELPER_STILL_TICKS,
    MAX_REJECTIONS,
    SPEECH_RANGE,
    greeted_npc_ids,
    in_sight,
    spoken_npc_ids,
)
from ..poll_cadence import THREAT_NEAR_BLOCKS
from ..config import Policy
from ..survival import hostiles_in_range, known_hostile, on_safe_tile, recently_attacked
from ..world import Entity, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import say_to

GREET_STATE = "Greet"

# A greeting whose result has not come back yet is not sent again for this
# many ticks (3 s); the runner records an applied one in ``greeted_npcs``.
GREET_RETRY_TICKS = 30


def threat_near(w: WorldModel, policy: Policy) -> bool:
    """A hostile hit us recently, a known hostile stands within a few blocks, or,
    off safe ground, something ``policy.hostile`` names is in ``hostile_range``."""
    if recently_attacked(w):
        return True
    assert w.pos is not None
    if any(
        e.kind in ("npc", "character") and known_hostile(w, e) and chebyshev(e.pos, w.pos) <= THREAT_NEAR_BLOCKS
        for e in w.entities
    ):
        return True
    return not on_safe_tile(w) and bool(hostiles_in_range(w, policy))


def likely_helper(w: WorldModel, e: Entity) -> bool:
    """``e`` looks like a helper: an NPC that has stood still ``HELPER_STILL_TICKS``
    in view and has not shown itself hostile. Only a guess: the API names no helpers."""
    return e.kind == "npc" and not known_hostile(w, e) and w.npc_still_ticks(e) >= HELPER_STILL_TICKS


def npc_to_greet(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The nearest NPC in sight to greet now, or None.

    It is a ``likely_helper``, not yet spoken to, and greeted fewer than
    ``MAX_REJECTIONS`` times, the last one at least ``GREET_RETRY_TICKS`` ago.
    Off safe ground, an NPC that ``policy.hostile`` covers is never greeted,
    at any range: a monster that has not moved yet is still a monster.
    Nothing while a threat is near, our speech cooldown still runs, or the
    plan's top op is a ``say`` (Investigate says that op's own text).
    """
    m = ctx.memory
    if w.pos is None or w.map_id is None:
        return None
    if ctx.plan is not None and (op := ctx.plan.current()) is not None and op["op"] == "say":
        return None
    if m.last_speech_tick is not None and w.tick - m.last_speech_tick < SPEECH_INTERVAL_TICKS:
        return None
    if threat_near(w, ctx.policy):
        return None
    if not on_safe_tile(w) and "npc" in ctx.policy.hostile:
        return None
    spoken = spoken_npc_ids(ctx.knowledge) | greeted_npc_ids(ctx.knowledge)
    here = w.pos

    def due(e: Entity) -> bool:
        sent, last = m.greetings.get(e.id, (0, None))
        return sent < MAX_REJECTIONS and (last is None or w.tick - last >= GREET_RETRY_TICKS)

    candidates = [
        e
        for e in w.entities
        if likely_helper(w, e)
        and e.id not in spoken
        and chebyshev(e.pos, here) <= SPEECH_RANGE
        and in_sight(w, w.map_id, here, e.pos)
        and due(e)
    ]
    return min(candidates, key=lambda e: (chebyshev(e.pos, here), e.id), default=None)


class GreetState(State):
    """Reflex, last: ``Say`` hello once to a nearby likely helper not yet spoken to.

    One intent, one tick: the walk under way resumes on the next decision.
    It fires only at a decision window: it is not a reflex for the runner's
    held-queue probe, so a walk queue already running is never dropped for it.
    """

    name = GREET_STATE

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive:
            return False
        return npc_to_greet(world, ctx) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        npc = npc_to_greet(world, ctx)
        if npc is None:
            return StateOutcome(None, "no one to greet", state=self.name)
        sent, _ = ctx.memory.greetings.get(npc.id, (0, None))
        ctx.memory.greetings[npc.id] = (sent + 1, world.tick)
        return StateOutcome([say_to(npc, GREET_TEXT)], f"greet npc {npc.id} ({npc.code or '?'})", state=self.name)
