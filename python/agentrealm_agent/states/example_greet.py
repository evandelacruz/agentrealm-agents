"""Example: say hello once to each NPC in sight (A51 teaching state).

Turn on with ``[flags] example_greet = true`` in the character directives file.
Off by default so the reference agent is unchanged.
"""

from __future__ import annotations

from ..interest_list import in_sight
from ..investigation import spoken_npc_ids
from ..world import Entity, WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import say_to


def example_greet_enabled(ctx: PlayContext) -> bool:
    return bool(ctx.directives.flags.get("example_greet"))


def next_npc_to_greet(w: WorldModel, ctx: PlayContext) -> Entity | None:
    if w.pos is None:
        return None
    spoken = spoken_npc_ids(ctx.knowledge)
    here = w.pos
    in_range = [
        e
        for e in w.entities
        if e.kind == "npc" and e.id not in spoken and in_sight(w, w.map_id, here, e.pos)
    ]
    if not in_range:
        return None
    return min(in_range, key=lambda e: abs(e.pos[0] - here[0]) + abs(e.pos[1] - here[1]))


class ExampleGreetState(State):
    name = "ExampleGreet"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            example_greet_enabled(ctx)
            and ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and next_npc_to_greet(world, ctx) is not None
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        npc = next_npc_to_greet(world, ctx)
        if npc is None:
            return StateOutcome(None, "no npc to greet", state=self.name)
        return StateOutcome([say_to(npc)], f"greet npc {npc.id}", state=self.name)
