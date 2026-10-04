"""Drive the real decision loop over a navigation scenario (A15).

Each decision goes through ``brain.decide`` (the state dispatcher). The move
it picks is applied against the true map the way the runner applies a Step
result: an applied move updates the position and calls ``on_step``; a
rejected one calls ``on_rejection`` and then ``learn_step_rejection`` with the
code the server would send.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.interest_list import MAX_REJECTIONS, say_key
from agentrealm_agent.navigation import learn_step_rejection
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.world import WALKABLE, Entity, Pos, WorldModel, chebyshev

from .grids import Scenario

TICKS_PER_DECISION = 4  # one Step at 2.5 blocks/s and 10 ticks/s


@dataclass
class Run:
    outcome: str  # reached | abandoned | budget
    moves: int
    world: WorldModel
    memory: Memory
    trace: list[dict] = field(default_factory=list)

    @property
    def signal(self) -> dict | None:
        sigs = self.memory.nav_stuck.stuck_signals
        return sigs[0] if sigs else None


def reveal(w: WorldModel, sc: Scenario) -> None:
    """A terrain read: the perception square around us, from the true map."""
    x0, y0 = w.pos
    r = sc.perception
    for y in range(y0 - r, y0 + r + 1):
        for x in range(x0 - r, x0 + r + 1):
            w.view.tiles[(x, y)] = sc.block((x, y))
    w.terrain_center, w.terrain_map = w.pos, w.map_id


def world_for(sc: Scenario) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=sc.start, perception=sc.perception)
    w.map_level = sc.level
    w.entities = [Entity("npc", 100 + i, p, code="parked") for i, p in enumerate(sc.npcs)]
    reveal(w, sc)
    return w


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", False)
    kw.setdefault("hostile", [])  # parked NPCs are obstacles here, not fights
    return Policy(kind="scripted", **kw)


def apply(w: WorldModel, m: Memory, sc: Scenario, cell: Pos) -> bool:
    """Apply one Step toward ``cell`` as the runner would. True when it moved."""
    occupied = any(e.pos == cell for e in w.entities)
    if chebyshev(w.pos, cell) == 1 and sc.block(cell) in WALKABLE and not occupied:
        w.pos = cell
        if m.path and m.path[0] == cell:
            m.path = m.path[1:]
        nav_stuck.on_step(m, w)
        reveal(w, sc)
        return True
    nav_stuck.on_rejection(m)
    code = "block_occupied" if occupied else "not_traversable"
    learn_step_rejection(m, w, None, cell, code, w.tick)
    return False


def run(
    sc: Scenario,
    policy: Policy,
    *,
    max_decisions: int = 400,
    memory: Memory | None = None,
    stop_on_signal: bool = True,
    seed: int = 7,
) -> Run:
    """Decide and apply until the goal is reached or given up on. A decision
    that sends nothing still lets the clock run."""
    w = world_for(sc)
    m = memory or Memory()
    for e in w.entities:  # already greeted: a parked NPC is only an obstacle here
        m.investigate_rejections[say_key(e.id)] = MAX_REJECTIONS
    rng = random.Random(seed)
    trace: list[dict] = []
    moves = 0
    for _ in range(max_decisions):
        if w.pos == sc.goal:
            return Run("reached", moves, w, m, trace)
        if stop_on_signal and m.nav_stuck.stuck_signals:
            return Run("abandoned", moves, w, m, trace)
        d = decide(w, m, policy, rng)
        row = {"tick": w.tick, "pos": list(w.pos), "intent": d.intent, "reason": d.reason}
        trace.append(row)
        if d.intent is not None and d.intent.get("verb") == "SetPosition":
            row["applied"] = apply(w, m, sc, (d.intent["x"], d.intent["y"]))
            moves += row["applied"]
        w.tick += TICKS_PER_DECISION
    return Run("budget", moves, w, m, trace)
