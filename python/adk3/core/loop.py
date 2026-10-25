"""The loop (docs/v3/SPEC.md **Architecture**): sense, keep milestones, replan when due, run the task, act.

One pass per decision the body says is due::

    s = body.sense()
    ledger keeps the milestones s shows
    a new plan, if one landed; ask for one when a trigger fires
    task = executive.step(s)
    action = best of body.actions(s) + task.actions(s), by the controller
    body.act(action)
    trace

The run stops itself after ``stall_minutes`` with no milestone and writes
its report, so nobody watches a character do nothing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .controller import Controller, Scored
from .executive import Executive
from .interfaces import Action, Body, Context, Report, State, Strategist, Task
from .ledger import Ledger
from .plan import Step
from .strategist import Asker, Triggers


@dataclass
class Settings:
    stall_minutes: float = 10.0  # no milestone this long: stop and report
    task_stall_seconds: float = 45.0  # a task's progress flat this long: it failed
    min_replan_seconds: float = 10.0
    max_plan_age_seconds: float = 120.0
    status_seconds: float = 1.0


@dataclass
class Agent:
    """What an agent fills in on top of a body: how it thinks."""

    strategist: Strategist
    build_task: Callable[[Step], Task]
    controller: Controller


@dataclass
class Run:
    body: Body
    agent: Agent
    ledger: Ledger
    trace_path: Path | None = None
    settings: Settings = field(default_factory=Settings)
    out: Callable[[str], None] = print

    def __post_init__(self) -> None:
        st = self.settings
        self.executive = Executive(self.agent.build_task, st.task_stall_seconds)
        self.asker = Asker(self.agent.strategist)
        self.triggers = Triggers(st.min_replan_seconds, st.max_plan_age_seconds)
        self.events: list[str] = []  # why the plan may be wrong, kept until the strategist is asked
        self.note = ""  # the last reply's problem
        self.stopped_why = ""
        self.plans: list[str] = []
        self._before: State | None = None
        self._status_at = 0.0
        self._trace = open(self.trace_path, "a", buffering=1) if self.trace_path else None
        self._last: Scored | None = None

    def run(self, stop: Callable[[], bool]) -> str:
        """Play until ``stop`` or a stall; returns why it stopped."""
        try:
            while not stop():
                s = self.body.sense()
                if s is not None:
                    self.decide(s, time.time())
                if self.ledger.minutes_since_last() >= self.settings.stall_minutes:
                    self.stopped_why = f"no milestone for {self.settings.stall_minutes:.0f} minutes"
                    break
            else:
                self.stopped_why = self.stopped_why or "stopped"
        finally:
            self.ledger.save()
            self.out(self.report())
            if self._trace:
                self._trace.close()
        return self.stopped_why

    def decide(self, s: State, now: float) -> Action | None:
        for key, label in self.body.milestones(s):
            if self.ledger.record(key, label, now):
                self.out(f"milestone: {label}")
        if self._before is not None:
            self.events += self.body.surprises(self._before, s)
        self._before = s

        self.take_plan(now)
        ex = self.executive
        task = ex.step(s, now)
        self.events += [f"task {o.step} failed: {o.why}" for o in ex.outcomes[len(ex.outcomes) - ex.take_outcomes():]
                        if o.result == "failed"]
        self.maybe_ask(s, now)

        risk = ex.plan.risk if ex.plan is not None else 0.0
        actions = self.body.actions(s) + (task.actions(s) if task is not None else [])
        ranked = self.agent.controller.rank(actions, s, Context(task, risk))
        best = ranked[0] if ranked else None
        self.body.act(best.action if best else None)
        self._last = best
        self.write(s, task, ranked, now)
        return best.action if best else None

    def take_plan(self, now: float) -> None:
        plan, error = self.asker.take()
        if error:
            self.note = error
            self.out(f"strategist: {error}")
        if plan is None:
            return
        self.executive.adopt(plan, now)
        self.plans.append(plan.goal)
        self.out(f"plan: {plan.goal} :: {' > '.join(str(st) for st in plan.steps)} (risk {plan.risk})")
        if self._trace:
            self._trace.write(json.dumps({"t": now, "plan": {"goal": plan.goal, "why": plan.why, "risk": plan.risk,
                                                             "steps": [[st.kind, st.args, st.why] for st in plan.steps]}}) + "\n")

    def maybe_ask(self, s: State, now: float) -> None:
        ex = self.executive
        reason = self.triggers.reason(now, no_plan=ex.plan is None, plan_over=ex.finished(), events=self.events)
        if reason is None or self.asker.busy():
            return
        report = Report(reason, ex.plan, ex.task.step if ex.task else None, list(ex.outcomes),
                        self.ledger.recent(), self.ledger.minutes_since_last(now), self.note)
        if self.asker.ask(s, report):
            self.triggers.asked(now)
            self.events, self.note = [], ""

    def write(self, s: State, task: Task | None, ranked: list[Scored], now: float) -> None:
        if self._trace:
            self._trace.write(json.dumps({"t": now, "self": self.body.status(s), "task": str(task.step) if task else None,
                                          "top": [x.brief() for x in ranked[:3]]}) + "\n")
        if now - self._status_at >= self.settings.status_seconds:
            self._status_at = now
            self.out(self.status_line(s, task, now))

    def status_line(self, s: State, task: Task | None, now: float) -> str:
        ex, best = self.executive, self._last
        goal = ex.plan.goal if ex.plan else "(asking)"
        doing = f"{best.action} ({best.total:+.2f})" if best else "-"
        return (f"{goal} | {task.step if task else '-'} | {doing} | {self.body.status(s)} | "
                f"last milestone {self.ledger.minutes_since_last(now):.1f}m ago | {self.ledger.per_hour(now):.1f}/h")

    def report(self) -> str:
        ex, led = self.executive, self.ledger
        lines = [f"run over: {self.stopped_why or 'stopped'}",
                 f"milestones this run: {len(led.run)} ({led.per_hour():.1f}/h): {', '.join(led.recent(20)) or 'none'}",
                 f"plans: {len(self.plans)}: {' | '.join(self.plans[-10:])}"]
        lines += [f"  {o.result}: {o.step} {o.why}".rstrip() for o in ex.outcomes]
        return "\n".join(lines)
