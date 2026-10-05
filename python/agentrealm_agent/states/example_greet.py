"""Example: say hello once to each other player in sight (A51 teaching state).

Not in the shipped ``STATES``: the reference agent never runs it. Add one line
to ``STATES`` in your own copy to try it (docs/MAKE_IT_YOURS.md).

No shipped state speaks to characters (Investigate only says hello to NPCs), so
this adds behavior rather than shadowing it. The runner sends the ``Say``
through the speech pacer, and it rides the window's ``POST tick``: no extra call.
"""

from __future__ import annotations

from ..interest_list import in_sight
from ..world import Entity, WorldModel
from .base import PlayContext, State, StateOutcome


def say_to_character(e: Entity, text: str = "hello") -> dict:
    # Say names a character recipient by a top-level character_id (API rules § Say).
    return {"verb": "Say", "character_id": e.id, "text": text}


class ExampleGreetState(State):
    name = "ExampleGreet"

    def __init__(self) -> None:
        # character id -> ids of characters it already greeted this run. One
        # instance serves every character the process runs, so key by our own id.
        # Marked when the Say is sent: at most one hello each, even if refused.
        self.greeted: dict[int, set[int]] = {}

    def next_to_greet(self, w: WorldModel) -> Entity | None:
        if w.pos is None:
            return None
        done = self.greeted.get(w.character_id, set())
        here = w.pos
        near = [
            e
            for e in w.entities
            if e.kind == "character" and e.id not in done and in_sight(w, w.map_id, here, e.pos)
        ]
        if not near:
            return None
        return min(near, key=lambda e: (abs(e.pos[0] - here[0]) + abs(e.pos[1] - here[1]), e.id))

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind == "scripted" and world.alive and self.next_to_greet(world) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        other = self.next_to_greet(world)
        if other is None:
            return StateOutcome(None, "no one to greet", state=self.name)
        self.greeted.setdefault(world.character_id, set()).add(other.id)
        return StateOutcome([say_to_character(other)], f"greet character {other.id}", state=self.name)
