"""Greet: say hello once to a nearby NPC not yet spoken to (reflex, A64).

The cheap, deterministic half of talking to NPCs, in the spirit of Pickup:
it acts on what is in sight now and never walks. ``Say`` reaches
``SPEECH_RANGE`` blocks, further than sight, so every NPC in sight is in
reach. Walking to an NPC is **Investigate**'s, for a planner ``say`` op.

A helper's reply arrives as a ``SpokenTo`` event, which ``clues.py`` stores
as a clue for the planner. An applied ``Say`` lands the NPC in the knowledge
base's ``spoken_npcs`` (the runner records it), so each NPC is greeted once.
"""

from __future__ import annotations

from ..executor.pacing import SPEECH_INTERVAL_TICKS
from ..investigation import MAX_REJECTIONS, SPEECH_RANGE, in_sight, spoken_npc_ids
from ..poll_cadence import THREAT_NEAR_BLOCKS
from ..config import Policy
from ..survival import hostiles_in_range, known_hostile, on_safe_tile, recently_attacked
from ..world import Entity, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import say_to

# A greeting whose result has not come back yet is not sent again for this
# many ticks (3 s); the runner records an applied one in ``spoken_npcs``.
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


def npc_to_greet(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The nearest NPC in sight to greet now, or None.

    It is not known hostile, not yet spoken to, and greeted fewer than
    ``MAX_REJECTIONS`` times, the last one at least ``GREET_RETRY_TICKS`` ago.
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
    spoken = spoken_npc_ids(ctx.knowledge)
    here = w.pos

    def due(e: Entity) -> bool:
        sent, last = m.greetings.get(e.id, (0, None))
        return sent < MAX_REJECTIONS and (last is None or w.tick - last >= GREET_RETRY_TICKS)

    candidates = [
        e
        for e in w.entities
        if e.kind == "npc"
        and e.id not in spoken
        and not known_hostile(w, e)
        and chebyshev(e.pos, here) <= SPEECH_RANGE
        and in_sight(w, w.map_id, here, e.pos)
        and due(e)
    ]
    return min(candidates, key=lambda e: (chebyshev(e.pos, here), e.id), default=None)


class GreetState(State):
    """Reflex, last: ``Say`` hello once to a nearby NPC not yet spoken to.

    One intent, one tick: the walk under way resumes on the next decision.
    It does not drop a walk queue already running; it waits for the next
    decision window.
    """

    name = "Greet"

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
        return StateOutcome([say_to(npc)], f"greet npc {npc.id} ({npc.code or '?'})", state=self.name)
