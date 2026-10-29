"""Lou on the adk3 loop: the body, her memory, her reactions and her strategist, wired together."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adk3.core.executive import Publication
from adk3.core.loop import Briefing
from adk3.realm import actions, milestones, travel
from adk3.realm.body import Body
from adk3.realm.context import Ctx
from adk3.realm.value import Levers

from . import publish, registry
from .strategist import Strategist


@dataclass(frozen=True)
class Standing:
    """The fallback's goal: never met, it runs until the strategist answers."""

    text: str

    def met(self, ctx: Ctx) -> bool:
        return False


class Lou:
    def __init__(self, body: Body, levers: Levers, strategist: Strategist | None, trace: Path,
                 out: Callable[[str], None] = print):
        self.body, self.levers, self.strategist, self.out = body, levers, strategist, out
        self.engines = registry.ENGINES
        self.tactics: dict[str, Any] = {}
        self.trace = open(trace, "a", buffering=1)

    # What the loop asks of an agent.

    def sense(self) -> Ctx | None:
        state = self.body.sense()
        if state is None:
            return None
        return Ctx(state, self.body.memory, self.body.kb, self.body.nav, self.body.town, self.levers, self.tactics)

    def act(self, action) -> None:
        self.body.act(action)

    def fallback(self, ctx: Ctx) -> tuple[Standing, publish.Approach]:
        """Safe, useful work until the strategist answers: eat when hurt, else cut grass near where she stands."""
        s = ctx.state
        if s.health < self.levers.heal_below * s.max_health:
            return Standing("heal until the strategist answers"), publish.Approach("forage", "forage", {}, {}, {})
        return Standing("earn gems until the strategist answers"), publish.Approach("cut grass nearby", "earn_gems", {}, {}, {})

    def running(self, ctx: Ctx, approach: Any) -> None:
        self.tactics = dict(getattr(approach, "tactics", None) or {})
        ctx.tactics, ctx.approach = self.tactics, approach

    def park(self, ctx: Ctx):
        """Walk to the nearest safe ground (Manual §11 Combat: no damage lands there); None once on it."""
        s = ctx.state
        if s.on_safe_ground:
            return None
        path = travel.to_safe(ctx)
        return actions.Walk(tuple(path), "to safe ground before stopping") if path else actions.Stop("no safe ground known")

    def milestones(self, ctx: Ctx) -> list[tuple[str, str]]:
        return milestones.shown(ctx)

    def surprises(self, ctx: Ctx) -> list[str]:
        return self.body.take_surprises() + ctx.memory.take_news()

    def known(self, ctx: Ctx) -> bool:
        return ctx.state.world.inventory_read  # until then gems and gear read as none

    def strategize(self, briefing: Briefing) -> Any:
        if self.strategist is None:
            return None
        return self.strategist(briefing)

    def publication(self, answer: Any, ctx: Ctx) -> Publication | None:
        if not isinstance(answer, dict):
            return None
        p = publish.publication(answer, ctx)
        lines = [f"strategist: {p.goal.text} by {p.approach.text}" + (f" — {p.why}" if p.why else "")]
        if p.plan_b is not None:
            lines.append(f"  plan B: {p.plan_b.text}")
        if p.next is not None:
            lines.append(f"  next: {p.next.goal.text} by {p.next.approach.text}")
        self.out("\n".join(lines))
        return p

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
