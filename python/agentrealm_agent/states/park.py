"""Park: walk to safe ground before the run exits (A66).

Runs only in the runner's park phase (``Memory.parking``, ``park.py``), when
the scenario is over. Dispatch then runs the survival reflexes and Park,
nothing else (``dispatch.PARK_STATES``). Park walks Retreat's path to the
nearest known safe tile a path reaches, else the town cell
(``retreat_step``), whether or not anything threatens. A safe tile it gets
no nearer to in a stuck window is ruled out for a while, and it moves on to
the next, else town (``retreat.no_progress``). One the planner finds no step
toward, the town cell included, is ruled out at once; once all are, the
runner ends the park (``park.PARK_NO_PATH``).
"""

from __future__ import annotations

from ..park import parked
from ..survival import retreat_goal
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .retreat import retreat_step


class ParkState(State):
    """Below the survival reflexes, in the park phase only. Walks to safety with ``retreat_step``."""

    name = "Park"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        m = ctx.memory
        if not m.parking or not world.alive or world.pos is None:
            return False
        return retreat_goal(world, ctx.knowledge) is not None and not parked(world, ctx.knowledge)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return retreat_step(world, ctx, self.name)
