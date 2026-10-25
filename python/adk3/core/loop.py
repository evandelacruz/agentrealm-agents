"""The loop (docs/v3/SPEC.md **Architecture**).

Each decision the game binding says is due::

    ctx = agent.sense()                 # the moment, and what is known
    ledger keeps the milestones it shows
    take the strategist's answer if one landed; ask again when due
    path = graph.focus(ctx)             # what to work on now
    skill = executive.step(path)        # how
    engine = first engine whose situation holds
    agent.act(engine.act(ctx, skill.order(ctx)))

The run stops itself after ``stall_minutes`` with no milestone and prints
its report, so nobody watches a character do nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .asker import Asker
from .engines import Engine, pick
from .ledger import Ledger
from .needs import Graph, Row
from .skills import Executive, Outcome


class Agent(Protocol):
    """What the game binding and the agent fill in."""

    graph: Graph
    engines: list[Engine]

    def sense(self) -> Any:
        """Spend this window; the decision context when a decision is due, else None."""

    def act(self, action: Any) -> None: ...

    def revision(self, ctx: Any) -> int:
        """A number that rises whenever what the agent knows changes; it lifts blocks."""

    def milestones(self, ctx: Any) -> list[tuple[str, str]]: ...

    def surprises(self, ctx: Any) -> list[str]:
        """What happened since the last decision that may change the strategist's mind."""

    def strategize(self, briefing: Briefing) -> Any:
        """The strategist call (run off the decision thread)."""

    def adopt(self, answer: Any) -> None:
        """Take the strategist's answer: its judgments, choices and tactics."""

    def status(self, ctx: Any) -> str: ...

    def record(self, entry: dict) -> None:
        """Write one trace record."""


@dataclass
class Briefing:
    """What the strategist is asked with."""

    ctx: Any
    reason: str
    rows: list[Row]
    outcomes: list[Outcome]
    milestones: list[str]
    minutes_since_milestone: float
    note: str = ""  # the last answer's problem, if it was refused


@dataclass
class Settings:
    stall_minutes: float = 15.0
    skill_stall_seconds: float = 60.0
    min_ask_seconds: float = 8.0
    max_answer_age_seconds: float = 180.0
    status_seconds: float = 1.0


@dataclass
class Run:
    agent: Agent
    ledger: Ledger
    settings: Settings = field(default_factory=Settings)
    out: Callable[[str], None] = print

    def __post_init__(self) -> None:
        self.executive = Executive(self.agent.graph, self.settings.skill_stall_seconds)
        self.asker = Asker(self.agent.strategize)
        self.pending: list[str] = ["starting"]  # why the strategist should be asked, kept until it is
        self.asked_at = float("-inf")
        self.answered = False
        self.note = ""
        self.stopped_why = ""
        self._status_at = 0.0
        self._outcomes_seen = 0

    def run(self, stop: Callable[[], bool]) -> str:
        """Play until ``stop`` or a stall; returns why it stopped."""
        try:
            while not stop():
                ctx = self.agent.sense()
                if ctx is not None:
                    self.decide(ctx, time.time())
                if self.ledger.minutes_since_last() >= self.settings.stall_minutes:
                    self.stopped_why = f"no milestone for {self.settings.stall_minutes:.0f} minutes"
                    break
            else:
                self.stopped_why = "stopped"
        finally:
            self.ledger.save()
            self.out(self.report())
        return self.stopped_why

    def decide(self, ctx: Any, now: float) -> None:
        agent = self.agent
        for key, label in agent.milestones(ctx):
            if self.ledger.record(key, label, now):
                self.out(f"milestone: {label}")
                self.pending.append(label)
        self.pending += agent.surprises(ctx)
        self.take_answer()

        revision = agent.revision(ctx)
        path = agent.graph.focus(ctx, revision)
        skill = self.executive.step(ctx, path, revision, now)
        for o in self.executive.outcomes[self._outcomes_seen:]:
            self.pending.append(f"{o.skill} for {o.need} {o.result}" + (f": {o.why}" if o.why else ""))
        self._outcomes_seen = len(self.executive.outcomes)
        if not path:
            self.pending.append("nothing in the needs graph can be worked now")
        self.maybe_ask(ctx, path, now)

        order = skill.order(ctx) if skill is not None else None
        engine = pick(agent.engines, ctx, order)
        action = engine.act(ctx, order) if engine is not None else None
        agent.act(action)
        agent.record({"t": now, "focus": path[-1].key if path else None, "skill": skill.name if skill else None,
                      "engine": engine.name if engine else None, "action": str(action) if action else None})
        if now - self._status_at >= self.settings.status_seconds:
            self._status_at = now
            focus = path[-1].title if path else "(nothing to work)"
            self.out(f"{focus} | {skill.name if skill else '-'} | {engine.name if engine else '-'}: "
                     f"{action or 'nothing'} | {agent.status(ctx)} | "
                     f"last milestone {self.ledger.minutes_since_last(now):.1f}m ago")

    def take_answer(self) -> None:
        answer, error = self.asker.take()
        if error:
            self.note = error
            self.out(f"strategist: {error}")
            self.pending.append("the last answer failed")
        if answer is not None:
            self.agent.adopt(answer)
            self.answered = True

    def maybe_ask(self, ctx: Any, path: list, now: float) -> None:
        if self.asker.busy() or now - self.asked_at < self.settings.min_ask_seconds:
            return
        if now - self.asked_at >= self.settings.max_answer_age_seconds:
            self.pending.append("the last answer is old")
        if not self.pending:
            return
        reason = "; ".join(dict.fromkeys(self.pending))
        rows = self.agent.graph.rows(ctx, path)
        briefing = Briefing(ctx, reason, rows, list(self.executive.outcomes), self.ledger.recent(),
                            self.ledger.minutes_since_last(now), self.note)
        if self.asker.ask(briefing):
            self.asked_at, self.pending, self.note = now, [], ""

    def report(self) -> str:
        lines = [f"run over: {self.stopped_why or 'stopped'}",
                 f"milestones this run: {len(self.ledger.run)} ({self.ledger.per_hour():.1f}/h): "
                 f"{', '.join(self.ledger.recent(30)) or 'none'}"]
        lines += [f"  {o.result}: {o.skill} for {o.need} {o.why}".rstrip() for o in self.executive.outcomes]
        lines += [f"  blocked: {k}: {b.why}" for k, b in self.agent.graph.blocks.items()]
        return "\n".join(lines)
