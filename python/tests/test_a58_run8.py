"""A58 run 8 offline: a reached goto is satisfied, not owed again on step-off (A16).

Live run 8 reached its 150-block goto and stood on the target. Each time
Explore stepped off toward a frontier, the goto was owed again and walked it
back: the two ping-ponged until the oscillation guard had given up four
explore targets and the smoke run aborted.

Rebuilt here through the real dispatcher and planner: a walled corridor with
the goto at its east end and fog past its west end. The agent walks to the
goto, then Explore must walk west into the fog and keep going.
"""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import WorldModel

GOTO = (11, 0)
WEST_END = -60  # the corridor runs on into the fog this far


def reveal(w: WorldModel) -> None:
    """What the agent sees: the corridor within ``perception`` of where it stands."""
    x0 = w.pos[0]
    for x in range(max(WEST_END, x0 - w.perception), min(GOTO[0], x0 + w.perception) + 1):
        w.view.tiles[(x, -1)] = w.view.tiles[(x, 1)] = "stone"
        w.view.tiles[(x, 0)] = "dirt"
    w.view.tiles[(GOTO[0] + 1, 0)] = "stone"
    w.terrain_center, w.terrain_map = w.pos, 1


def world(at=(9, 0)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=6, gems=10, health=10, max_health=10)
    reveal(w)
    return w


def ctx(*, plan: bool) -> PlayContext:
    policy = Policy(kind="scripted", goals=["goto", "explore"], goto=GOTO)
    params = dict(PARAM_DEFAULTS)
    return PlayContext(
        Memory(),
        policy,
        random.Random(0),
        params=params,
        knowledge=KnowledgeBase.empty("sandbox"),
        plan=Plan.from_policy(policy, params) if plan else None,
    )


def land(w: WorldModel, c: PlayContext, out) -> None:
    """The decision's SetPosition lands; the runner trims the path and counts the move."""
    w.tick += 10
    if out.intents and out.intents[0]["verb"] == "SetPosition":
        w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        if c.memory.path[:1] == [w.pos]:
            c.memory.path = c.memory.path[1:]
        nav_stuck.on_step(c.memory, w)
    reveal(w)


class ReachedGotoThenExploreTest(unittest.TestCase):
    def _play(self, *, plan: bool, decisions: int = 40):
        w, c = world(), ctx(plan=plan)
        reached_at = None
        after = []  # (cell, reason) of each decision once the goto was reached
        for i in range(decisions):
            out = dispatch(w, c)
            if reached_at is not None:
                after.append((w.pos, out.reason))
            land(w, c, out)
            if reached_at is None and w.pos == GOTO:
                reached_at = i
        return w, c, reached_at, after

    def test_explore_walks_away_from_the_reached_goto(self):
        for plan in (False, True):
            with self.subTest(plan=plan):
                w, c, reached_at, after = self._play(plan=plan)
                self.assertIsNotNone(reached_at, "the goto was reached")
                goto_moves = [r for _, r in after if r.startswith("goto")]
                self.assertEqual(goto_moves, [], "no goto walk back after it was reached")
                self.assertEqual(c.memory.nav_stuck.oscillations, [], "no pacing")
                self.assertEqual(
                    [s for s in c.memory.nav_stuck.stuck_signals if s.get("reason") == "pacing"],
                    [],
                    "no oscillation give-up",
                )
                self.assertLessEqual(w.pos[0], GOTO[0] - 20, "Explore kept walking west")


if __name__ == "__main__":
    unittest.main()
