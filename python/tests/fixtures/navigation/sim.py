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
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import view_from_kb
from agentrealm_agent.navigation import learn_step_rejection
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.navigation.stuck import active as nav_active
from agentrealm_agent.navigation.stuck import on_break_opened
from agentrealm_agent.break_memory import capabilities_for_code, record_attempt
from agentrealm_agent.item_table import use_target_block
from agentrealm_agent.plan import Plan
from agentrealm_agent.world import DOORS, WALKABLE, Entity, MapView, Pos, WorldModel, chebyshev

from .grids import GLYPHS, Scenario

TICKS_PER_DECISION = 4  # one Step at 2.5 blocks/s and 10 ticks/s

MAP2_ROWS = ("G.......",)


@dataclass(frozen=True)
class CrossMapRun:
    """Door on map 1, goal on map 2, with a recorded warp in the knowledge base."""

    door: Pos
    landing: Pos
    goal: Pos
    kb: KnowledgeBase


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


def map2_view() -> MapView:
    view = MapView()
    for y, row in enumerate(MAP2_ROWS):
        for x, g in enumerate(row):
            view.tiles[(x, y)] = GLYPHS[g]
    return view


def reveal(w: WorldModel, sc: Scenario, *, map2: MapView | None = None) -> None:
    """A terrain read: the perception square around us, from the true map."""
    x0, y0 = w.pos
    r = sc.perception
    for y in range(y0 - r, y0 + r + 1):
        for x in range(x0 - r, x0 + r + 1):
            if w.map_id == 1:
                w.view.tiles[(x, y)] = sc.block((x, y))
            elif map2 is not None:
                block = map2.tiles.get((x, y))
                if block is not None:
                    w.view.tiles[(x, y)] = block
    w.terrain_center, w.terrain_map = w.pos, w.map_id


def world_for(sc: Scenario, *, cross: CrossMapRun | None = None) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=sc.start, perception=sc.perception)
    w.map_level = sc.level
    w.entities = [Entity("npc", 100 + i, p, code="parked") for i, p in enumerate(sc.npcs)]
    if cross is not None:
        w.maps[1] = view_from_kb(cross.kb, 1)
        w.maps[2] = view_from_kb(cross.kb, 2)
    reveal(w, sc, map2=map2_view() if cross else None)
    return w


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", False)
    kw.setdefault("hostile", [])  # parked NPCs are obstacles here, not fights
    return Policy(kind="scripted", **kw)


def _tile(sc: Scenario, w: WorldModel, p: Pos, map2: MapView, overlay: dict[Pos, str]) -> str:
    if p in overlay:
        return overlay[p]
    if w.map_id == 1:
        return sc.block(p)
    return map2.tiles.get(p, "wall")


def apply_use(
    w: WorldModel,
    m: Memory,
    sc: Scenario,
    intent: dict,
    overlay: dict[Pos, str],
    *,
    knowledge: KnowledgeBase | None = None,
    map2: MapView | None = None,
) -> bool:
    block = use_target_block(intent, w.entities)
    if block is None or w.map_id is None:
        return False
    cap = m.break_pending[2] if m.break_pending else None
    armed = w.armed_code or ""
    caps = capabilities_for_code(armed)
    if cap and cap in caps:
        use_cap = cap
    elif "cut" in caps or "chop" in caps:
        use_cap = "cut"
    else:
        use_cap = next(iter(caps), "")
    tile = _tile(sc, w, block, map2 or map2_view(), overlay)
    if tile == "bush" and use_cap in ("cut", "chop"):
        overlay[block] = "dirt"
        w.view.tiles[block] = "dirt"
        record_attempt(
            knowledge,
            map_id=w.map_id,
            pos=block,
            capability=use_cap,
            result="opened",
            block_after="dirt",
            tick=w.tick,
        )
        m.break_pending = None
        on_break_opened(m, w, nav_active(m, w))
        return True
    record_attempt(
        knowledge,
        map_id=w.map_id,
        pos=block,
        capability=use_cap or "cut",
        result="applied_no_effect",
        tick=w.tick,
    )
    m.break_pending = None
    return True


def apply(
    w: WorldModel,
    m: Memory,
    sc: Scenario,
    cell: Pos,
    overlay: dict[Pos, str],
    *,
    cross: CrossMapRun | None = None,
    map2: MapView | None = None,
) -> bool:
    """Apply one Step toward ``cell`` as the runner would. True when it moved."""
    map2 = map2 or map2_view()
    occupied = any(e.pos == cell for e in w.entities if w.map_id == 1)
    block = _tile(sc, w, cell, map2, overlay)
    walkable = block in WALKABLE or block in DOORS
    if chebyshev(w.pos, cell) == 1 and walkable and not occupied:
        w.pos = cell
        if m.path and m.path[0] == cell:
            m.path = m.path[1:]
        nav_stuck.on_step(m, w)
        if cross is not None and w.map_id == 1 and cell == cross.door:
            w.map_id = 2
            w.pos = cross.landing
            w.maps.setdefault(2, map2)
            m.path, m.goal = [], ""
        reveal(w, sc, map2=map2)
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
    cross: CrossMapRun | None = None,
    armed_code: str | None = None,
) -> Run:
    """Decide and apply until the goal is reached or given up on. A decision
    that sends nothing still lets the clock run."""
    map2 = map2_view()
    w = world_for(sc, cross=cross)
    if armed_code:
        w.armed_code = armed_code
    m = memory or Memory()
    done_map = 2 if cross is not None else 1
    done_pos = cross.goal if cross is not None else sc.goal
    knowledge = cross.kb if cross is not None else None
    plan = Plan.from_policy(policy, dict(PARAM_DEFAULTS))  # the built-in planner (A34)
    rng = random.Random(seed)
    trace: list[dict] = []
    moves = 0
    overlay: dict[Pos, str] = {}
    for _ in range(max_decisions):
        if w.map_id == done_map and w.pos == done_pos:
            return Run("reached", moves, w, m, trace)
        if stop_on_signal and m.nav_stuck.stuck_signals:
            return Run("abandoned", moves, w, m, trace)
        d = decide(w, m, policy, rng, knowledge=knowledge, plan=plan)
        row = {"tick": w.tick, "pos": list(w.pos), "map_id": w.map_id, "intent": d.intent, "reason": d.reason}
        trace.append(row)
        if d.intent is not None and d.intent.get("verb") == "SetPosition":
            row["applied"] = apply(w, m, sc, (d.intent["x"], d.intent["y"]), overlay, cross=cross, map2=map2)
            moves += row["applied"]
        elif d.intent is not None and d.intent.get("verb") == "Use":
            row["applied"] = apply_use(w, m, sc, d.intent, overlay, knowledge=knowledge, map2=map2)
        w.tick += TICKS_PER_DECISION
    return Run("budget", moves, w, m, trace)
