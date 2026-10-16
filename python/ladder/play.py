"""Lorraine's play loop until A141: walk the tree, ask the one question at its goal, run the op.

Every tick window the tree is walked from the top (``goals.walk``), after
lifting the blocks whose awaited state has come (``ask.lift_changed``). The
planner is asked the one question (``ask.Planner``) only when the goal node
changes or the state a block may wait on does (``ask.watch_lines``), or,
when the last ask got no answer, once the backoff and budget allow; never
every tick. A ``cannot`` blocks the node and the next walk descends. With no
answer (no planner, the budget spent, backing off, a failed call) the
tree's own op runs.

Carrying the op out is minimal, and temporary: the sync, wake and flee are
Starter Stan's (``starter_agent``, her one tie to him); an op whose cell is
where she goes (``CELL_OPS``) is walked to over known ground and is done once
she stands there; anything else, an ``explore_area`` or ``gather_gems``
whose x, y names a region included, explores as Stan does. The question is
asked on the tick thread, so play waits while a call is out: up to the
provider client's timeout (600 s in the ``anthropic`` SDK) on a call that
hangs. A141 replaces all of this with the ADK's executor and her reflexes.
"""

from __future__ import annotations

from collections import deque

import starter_agent
from agentrealm_adk import config
from agentrealm_adk.client import Client
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.targets import GoalOp
from agentrealm_adk.world import Pos, WorldModel

from .ask import ALREADY_MET, Planner, already_met, answer, lift_changed, watch_lines
from .goals import Blocks, Node, Turn, tree, walk

# The ops whose x, y is the cell she goes to; any other op's x, y names a region.
CELL_OPS = frozenset({"travel", "fetch_item", "enter_level"})


def op_cell(op: GoalOp | None, w: WorldModel) -> Pos | None:
    """The cell an op of ``CELL_OPS`` goes to on the map she is on, else None."""
    if not op or op.get("op") not in CELL_OPS or not isinstance(op.get("x"), int) or not isinstance(op.get("y"), int):
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
        self.asked_for: tuple | None = None  # the goal and the state the last ask was about
        self.answered = False  # the planner answered that ask
        self.done: list[GoalOp] = []  # ops carried out since that ask: never run again for it

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
        """The op for the turn's goal: the planner's answer, else the tree's own
        op. Asked again only for a new goal or state, or after an ask with no answer."""
        w = self.world
        for key in lift_changed(self.blocks, w, self.knowledge):
            self.log("planner", f"{key}: block lifted, what it waited on came")
            self.asked_for = None  # the next walk asks afresh
        if self.op is not None and op_cell(self.op, w) == w.pos:
            self.done.append(self.op)  # there: the op is carried out
            self.op = None
        turn = walk(w, self.knowledge, self.blocks, self.root)
        if turn.goal is None:
            self.op, self.asked_for = None, None
            return None
        key = (turn.goal.key, tuple(watch_lines(w, self.knowledge).values()))
        if key != self.asked_for:
            self.asked_for, self.done = key, []
            self.op = self.choose(turn)
        elif not self.answered and self.planner.ready():
            self.op = self.choose(turn)
        return self.op

    def choose(self, turn: Turn) -> GoalOp | None:
        goal = turn.goal
        assert goal is not None
        reply = self.planner.ask(goal, turn.reading)
        self.answered = reply is not None
        if reply is None:
            if self.planner.last_error:
                self.log("planner", f"{goal.title}: {self.planner.last_error}; the tree's op")
                self.planner.last_error = ""
            op = turn.op()
            return None if op in self.done else op
        if already_met(reply, turn.reading.world):
            # No wait: a plain block to its expiry, and one more ask that says so.
            answer(goal, reply, self.blocks, turn.reading)
            self.log("planner", f"{goal.title}: cannot until {reply.until}, already true; asking once more")
            again = self.planner.ask(goal, turn.reading, note=ALREADY_MET.format(until=reply.until))
            if again is None or already_met(again, turn.reading.world):
                return None
            if again.op is not None:
                self.blocks.lift(goal.key)
            reply = again  # an op, or a cannot that waits on something not yet true
        op = answer(goal, reply, self.blocks, turn.reading)
        if op is None:
            b = self.blocks[goal.key]
            self.log("planner", f"{goal.title}: cannot: {reply.cannot} (until {b.named or 'tick'} {b.was or b.until})")
            return None  # the next walk descends past the blocked node
        self.log("planner", f"{goal.title}: {op}")
        return None if op in self.done else op
