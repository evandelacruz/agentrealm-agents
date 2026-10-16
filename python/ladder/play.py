"""Lorraine's play loop until A141: walk the tree, ask the one question at its goal, run the op.

Every tick window the tree is walked from the top (``goals.walk``), after
lifting the blocks whose named state changed (``ask.lift_changed``). At a
new goal, once its op's cell is reached, or every ``REASK_TICKS``, the
planner is asked the one question about it (``ask.Planner``); a ``cannot``
blocks the node and the next walk descends. With no answer (no planner, the
budget spent, backing off, a failed call) the tree's own op runs.

Carrying the op out is minimal, and temporary: the sync, wake and flee are
Starter Stan's (``starter_agent``, her one tie to him); an op that names a
cell on this map is walked to over known ground; anything else explores as
Stan does. The question is asked on the tick thread, so play waits while a
call is out. A141 replaces all of this with the ADK's executor and her
reflexes.
"""

from __future__ import annotations

from collections import deque

import starter_agent
from agentrealm_adk import config
from agentrealm_adk.client import Client
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.targets import GoalOp
from agentrealm_adk.world import Pos, WorldModel

from .ask import Planner, answer, lift_changed
from .goals import Blocks, Node, Turn, tree, walk

# Ask again about the same goal after this many ticks (30 s at 10 Hz).
REASK_TICKS = 300


def op_cell(op: GoalOp | None, w: WorldModel) -> Pos | None:
    """The cell an op names on the map she is on, else None."""
    if not op or not isinstance(op.get("x"), int) or not isinstance(op.get("y"), int):
        return None
    if op.get("map_id") not in (None, w.map_id):
        return None
    return op["x"], op["y"]


def path_to(world: WorldModel, goal: Pos, blocked: set[Pos]) -> list[Pos]:
    """Steps to ``goal`` over known walkable ground, breadth first; [] when none is known."""
    start = world.pos
    if start is None or start == goal:
        return []
    came_from: dict[Pos, Pos | None] = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        if here == goal:
            path = []
            while here != start:
                path.append(here)
                here = came_from[here]
            return path[::-1]
        for n in sorted(world.open_neighbours(here, blocked)):
            if n not in came_from:
                came_from[n] = here
                queue.append(n)
    return []


class LadderRunner(starter_agent.StarterRunner):
    """Stan's runner, with the tree choosing where to go (until A141)."""

    def __init__(self, cfg: config.CharacterConfig, client: Client, cid: int, stop, planner: Planner,
                 knowledge: KnowledgeBase | None = None, out=print):
        super().__init__(cfg, client, cid, stop, out)
        self.planner = planner
        self.knowledge = knowledge
        self.root: Node = tree()
        self.blocks = Blocks()
        self.op: GoalOp | None = None
        self.op_for = ""  # the key of the node the op works
        self.asked_tick = 0
        self.asked_at: Pos | None = None  # where she stood when the op was chosen

    def step(self, call: str) -> None:
        if call != "tick":
            super().step(call)
            return
        w, m = self.world, self.mem
        d = self.decide()
        r = self.client.tick(self.cid, d.intents, snapshot_version=w.snapshot_version)
        starter_agent.apply_tick(w, m, r, sent=d.intents)
        self.log("tick", f"{d.mode if d.intents else 'hold'}: {d.reason}")

    def decide(self) -> starter_agent.StarterDecision:
        """Stan's wake, cooldown and flee first; then a step toward the op's cell, else Stan's explore."""
        w, m = self.world, self.mem
        if w.asleep or not starter_agent.move_ready(w, m) or starter_agent.hostiles_in_range(w, m.policy):
            return starter_agent.decide(w, m, self.rng)
        op = self.goal_op()
        cell = op_cell(op, w)
        if cell is not None:
            path = path_to(w, cell, starter_agent.blocked_tiles(w, m.policy))
            if path and path[0] not in w.occupied():
                m.path = []
                return starter_agent.StarterDecision([starter_agent.set_position(path[0])], f"{op['op']} {cell}", "Goal")
        return starter_agent.decide(w, m, self.rng)

    def goal_op(self) -> GoalOp | None:
        """The op for the turn's goal: the planner's answer, else the tree's own op."""
        w = self.world
        for key in lift_changed(self.blocks, w, self.knowledge, self.root):
            self.log("planner", f"{key}: block lifted, its state changed")
        turn = walk(w, self.knowledge, self.blocks, self.root)
        if turn.goal is None:
            self.op, self.op_for = None, ""
            return None
        due = (
            turn.goal.key != self.op_for
            or self.op is None
            or op_cell(self.op, w) == w.pos != self.asked_at
            or w.tick - self.asked_tick >= REASK_TICKS
        )
        if due:
            self.op, self.op_for, self.asked_tick, self.asked_at = self.choose(turn), turn.goal.key, w.tick, w.pos
        return self.op

    def choose(self, turn: Turn) -> GoalOp | None:
        goal = turn.goal
        assert goal is not None
        reply = self.planner.ask(goal, turn.reading)
        if reply is None:
            if self.planner.last_error:
                self.log("planner", f"{goal.title}: {self.planner.last_error}; the tree's op")
                self.planner.last_error = ""
            return turn.op()
        op = answer(goal, reply, self.blocks, turn.reading)
        if op is None:
            self.log("planner", f"{goal.title}: cannot: {reply.cannot} (until {self.blocks[goal.key].named} changes)")
            return None  # the next walk descends past the blocked node
        self.log("planner", f"{goal.title}: {op}")
        return op
