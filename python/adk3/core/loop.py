"""The loop (docs/v3/SPEC.md **Architecture**).

Each decision the game binding says is due::

    ctx = agent.sense()                    # the moment, and what is known
    ledger keeps the milestones it shows
    adopt the strategist's publication if one landed; ask again when due
    skill = executive.step(ctx)            # the published approach, plan B, next, or the fallback
    agent.running(ctx, approach)           # the reactions play its tactics
    engine = first reaction whose situation holds (fixed priority)
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
from .executive import Executive, Publication, Report
from .ledger import Ledger


class Agent(Protocol):
    """What the game binding and the agent fill in."""

    engines: list[Engine]

    def sense(self) -> Any:
        """Spend this window; the decision context when a decision is due, else None."""

    def act(self, action: Any) -> None: ...

    def fallback(self, ctx: Any) -> tuple[Any, Any]:
        """The goal and approach to run when nothing published can be: safe, useful work."""

    def running(self, ctx: Any, approach: Any) -> None:
        """The approach under way this decision, so the reactions play its tactics."""

    def milestones(self, ctx: Any) -> list[tuple[str, str]]: ...

    def surprises(self, ctx: Any) -> list[str]:
        """What happened since the last decision that the strategist should hear: a death, a new clue, a reaction's outcome."""

    def known(self, ctx: Any) -> bool:
        """What the strategist is briefed on has been read in full (the first reads can lag a decision or two)."""

    def strategize(self, briefing: Briefing) -> Any:
        """The strategist call (run off the decision thread)."""

    def publication(self, answer: Any, ctx: Any) -> Publication | None:
        """The strategist's answer, as a publication; raises ValueError when it cannot be carried out."""

    def status(self, ctx: Any) -> str: ...

    def record(self, entry: dict) -> None:
        """Write one trace record."""


@dataclass
class Briefing:
    """What the strategist is asked with."""

    ctx: Any
    reason: str
    running: str  # the goal and approach under way
    reports: list[Report]
    milestones: list[str]
    minutes_since_milestone: float
    note: str = ""  # the last answer's problem, if it was refused


@dataclass
class Settings:
    stall_minutes: float = 15.0
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
        self.executive = Executive(self.agent.fallback)
        self.asker = Asker(self.agent.strategize)
        self.pending: list[str] = ["starting"]  # why the strategist should be asked, kept until it is
        self.asked_at = float("-inf")
        self.note = ""
        self.stopped_why = ""
        self._status_at = 0.0
        self._reports_seen = 0

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
        agent, ex = self.agent, self.executive
        for key, label in agent.milestones(ctx):
            if self.ledger.record(key, label, now):
                self.out(f"milestone: {label}")
        self.pending += agent.surprises(ctx)
        self.take_answer(ctx, now)

        skill = ex.step(ctx, now)
        for r in ex.reports[self._reports_seen:]:
            self.out(f"executive: {r}")
            if r.result != "done" or ex.plan == "fallback":
                self.pending.append(str(r))
        self._reports_seen = len(ex.reports)
        self.maybe_ask(now, ctx)
        agent.running(ctx, ex.approach)

        order = skill.order(ctx) if skill is not None else None
        engine = pick(agent.engines, ctx, order)
        action = engine.act(ctx, order) if engine is not None else None
        agent.act(action)
        agent.record({"t": now, "goal": ex.goal.text if ex.goal else None, "plan": ex.plan,
                      "approach": ex.approach.text if ex.approach else None, "engine": engine.name if engine else None,
                      "action": str(action) if action else None})
        if now - self._status_at >= self.settings.status_seconds:
            self._status_at = now
            goal = ex.goal.text if ex.goal else "-"
            self.out(f"{goal} | {ex.plan}: {ex.approach.text if ex.approach else '-'} | "
                     f"{engine.name if engine else '-'}: {action or 'nothing'} | {agent.status(ctx)} | "
                     f"last milestone {self.ledger.minutes_since_last(now):.1f}m ago")

    def take_answer(self, ctx: Any, now: float) -> None:
        answer, error = self.asker.take()
        if error:
            self.note = error
            self.out(f"strategist: {error}")
            self.pending.append("the last answer failed")
        if answer is None:
            return
        try:
            publication = self.agent.publication(answer, ctx)
        except ValueError as e:
            self.note = f"your answer could not be carried out: {e}"
            self.out(f"strategist: {self.note}")
            self.pending.append("the last answer could not be carried out")
            return
        if publication is not None:
            self.executive.adopt(publication, ctx, now)

    def maybe_ask(self, now: float, ctx: Any) -> None:
        if self.asker.busy() or now - self.asked_at < self.settings.min_ask_seconds or not self.agent.known(ctx):
            return
        if now - self.asked_at >= self.settings.max_answer_age_seconds:
            self.pending.append("time to look again")
        if not self.pending:
            return
        ex = self.executive
        running = f"{ex.goal.text} by {ex.approach.text} (plan {ex.plan})" if ex.goal else "nothing"
        briefing = Briefing(ctx, "; ".join(dict.fromkeys(self.pending)), running, list(ex.reports),
                            self.ledger.recent(), self.ledger.minutes_since_last(now), self.note)
        if self.asker.ask(briefing):
            self.asked_at, self.pending, self.note = now, [], ""

    def report(self) -> str:
        lines = [f"run over: {self.stopped_why or 'stopped'}",
                 f"milestones this run: {len(self.ledger.run)} ({self.ledger.per_hour():.1f}/h): "
                 f"{', '.join(self.ledger.recent(30)) or 'none'}"]
        lines += [f"  {r}" for r in self.executive.reports]
        return "\n".join(lines)
