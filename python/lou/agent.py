"""Lou on the adk3 loop: the body, her memory, the needs graph, her engines and her strategist, wired together."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adk3.core.engines import Judge
from adk3.core.loop import Briefing
from adk3.core.needs import Graph
from adk3.realm import milestones
from adk3.realm.body import Body
from adk3.realm.context import Ctx
from adk3.realm.value import Levers

from . import registry
from .engines import SWITCH_MARGIN, Scorer
from .strategist import Strategist, adopt


class Lou:
    def __init__(self, body: Body, levers: Levers, strategist: Strategist | None, trace: Path,
                 out: Callable[[str], None] = print):
        self.body, self.levers, self.strategist, self.out = body, levers, strategist, out
        self.graph = Graph(registry.ROOT)
        self.proposers = registry.PROPOSERS
        self.judge = Judge(Scorer(), SWITCH_MARGIN)
        self.judgments: dict[str, Any] = {}
        self.tactics: dict[str, Any] = {}
        self.trace = open(trace, "a", buffering=1)
        self._revision = 0
        self._seen: tuple | None = None

    # What the loop asks of an agent.

    def sense(self) -> Ctx | None:
        state = self.body.sense()
        if state is None:
            return None
        return Ctx(state, self.body.memory, self.body.kb, self.body.nav, self.body.town, self.levers,
                   self.judgments, self.tactics)

    def act(self, action) -> None:
        self.body.act(action)

    def revision(self, ctx: Ctx) -> int:
        """Rises whenever what Lou knows or holds changes: a new fact, a new map, a new item, a level."""
        s = ctx.state
        now = (ctx.memory.revision, s.map_id, tuple(sorted(c.code for c in s.carried)), s.levels_cleared, s.lives)
        if now != self._seen:
            self._seen, self._revision = now, self._revision + 1
        return self._revision

    def milestones(self, ctx: Ctx) -> list[tuple[str, str]]:
        return milestones.shown(ctx)

    def surprises(self, ctx: Ctx) -> list[str]:
        return self.body.take_surprises()

    def strategize(self, briefing: Briefing) -> Any:
        if self.strategist is None:
            return None
        return self.strategist(briefing)

    def adopt(self, answer: Any) -> None:
        if not isinstance(answer, dict):
            return
        ctx = Ctx(self.body.state(), self.body.memory, self.body.kb, self.body.nav, self.body.town, self.levers,
                  self.judgments, self.tactics)
        changed = adopt(ctx, answer)
        self.out(f"strategist: {answer.get('why', '')}".rstrip() + (f" [{', '.join(changed)}]" if changed else ""))

    def status(self, ctx: Ctx) -> str:
        s = ctx.state
        where = f"level {s.level}" if s.level else "overworld"
        gear = ",".join(c.code for c in s.carried if c.where != "held") or "-"
        return f"hp {s.health}/{s.max_health} lives {s.lives} gems {s.gems} [{gear}] {where} {s.map_id}:{s.pos[0]},{s.pos[1]}"

    def record(self, entry: dict) -> None:
        self.trace.write(json.dumps(entry, default=str) + "\n")

    def close(self) -> None:
        self.body.save()
        self.trace.close()

