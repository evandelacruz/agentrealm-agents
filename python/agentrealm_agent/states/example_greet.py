"""Example: say hello once to each other player in sight (A51 teaching state).

Opt-in: not in the shipped ``STATES``. Add one line there to try it
(docs/MAKE_IT_YOURS.md). The ``Say`` rides the window's ``POST tick``: no extra call.
"""

from __future__ import annotations

from ..interest_list import in_sight
from ..world import Entity, WorldModel
from .base import PlayContext, State, StateOutcome


class ExampleGreetState(State):
    name = "ExampleGreet"

    def __init__(self) -> None:
        # Our character id -> ids already greeted (one instance serves every character).
        self.greeted: dict[int, set[int]] = {}

    def next_to_greet(self, w: WorldModel) -> Entity | None:
        if w.pos is None:
            return None
        (x, y), done = w.pos, self.greeted.get(w.character_id, set())
        near = [e for e in w.entities
                if e.kind == "character" and e.id not in done and in_sight(w, w.map_id, w.pos, e.pos)]
        # Nearest first; ties go to the lower id.
        return min(near, key=lambda e: (abs(e.pos[0] - x) + abs(e.pos[1] - y), e.id), default=None)

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind == "scripted" and world.alive and self.next_to_greet(world) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        other = self.next_to_greet(world)
        if other is None:
            return StateOutcome(None, "no one to greet", state=self.name)
        self.greeted.setdefault(world.character_id, set()).add(other.id)  # once, even if refused
        say = {"verb": "Say", "character_id": other.id, "text": "hello"}
        return StateOutcome([say], f"greet character {other.id}", state=self.name)
