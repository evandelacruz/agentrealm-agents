"""The interfaces a game binding and an agent fill in (docs/v3/SPEC.md **Interfaces**).

The core never looks inside a State or an Action: it passes them between the
pieces. Everything game-shaped lives behind these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .plan import Plan, Step

State = Any  # one snapshot per decision; the binding defines it


@dataclass(eq=False)
class Action:
    """One thing the controller may do this decision.

    ``key`` names it across decisions, so an action still under way is let
    run instead of sent again. ``data`` is the binding's (intents, target cell).
    """

    kind: str
    key: tuple
    why: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.kind}{self.key[1:] if len(self.key) > 1 else ''}"


class Task(Protocol):
    """One plan step carried out. Built from a ``Step`` by the agent's task registry."""

    step: Step

    def done(self, s: State) -> bool: ...

    def failed(self, s: State) -> str | None:
        """Why the task cannot go on, or None."""

    def progress(self, s: State) -> float:
        """A number that rises as the task gets nearer done; flat for too long is a stall."""

    def actions(self, s: State) -> list[Action]:
        """The actions that work this task now (a walk to its target, a Take, a Read)."""

    def wants(self, a: Action, s: State) -> float:
        """How much ``a`` advances this task, 0 to 1."""


@dataclass
class Context:
    """What a consideration sees besides the action and the state."""

    task: Task | None
    risk: float


class Consideration(Protocol):
    """One reason to prefer an action, as a signed number: the change it makes to something the agent values."""

    name: str

    def score(self, a: Action, s: State, ctx: Context) -> float: ...


class Body(Protocol):
    """The game binding: senses and acts within the game's call budget."""

    def sense(self) -> State | None:
        """Spend this window: a read the world model is owed (None), or the State when a decision is due."""

    def act(self, a: Action | None) -> None:
        """Carry ``a`` out; one still under way runs on. None sends nothing."""

    def actions(self, s: State) -> list[Action]:
        """The actions open whatever the task: flee, fight, heal, pick up, wait."""

    def milestones(self, s: State) -> list[tuple[str, str]]:
        """The milestones ``s`` shows, as (key, label); the ledger keeps the new ones."""

    def surprises(self, before: State, s: State) -> list[str]:
        """What changed enough that the plan may be wrong: a death, a new map, a level cleared."""

    def status(self, s: State) -> str:
        """A few words of self for the status line: health, gems, where."""


class Strategist(Protocol):
    """Decides what to do and why. Slow; called off the decision thread."""

    def plan(self, s: State, report: Report) -> Plan | None: ...


@dataclass
class Outcome:
    """A task over: done, failed, or dropped for a new plan, with why."""

    step: Step
    result: str  # done | failed | dropped
    why: str = ""


@dataclass
class Report:
    """What the strategist is told besides the state: why it is asked, the plan so far and how its tasks went."""

    reason: str
    plan: Plan | None
    current: Step | None
    outcomes: list[Outcome]
    milestones: list[str]
    minutes_since_milestone: float
    note: str = ""  # the last reply's problem, when it was refused
