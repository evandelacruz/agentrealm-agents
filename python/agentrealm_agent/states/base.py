"""State protocol, the context states run in, and one dispatch round's outcome."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..client import Intent
from ..config import Policy
from ..directives import PARAM_DEFAULTS, Directives, default_directives
from ..executor import DEFAULT_TICK_RATE_HZ
from ..gem_yield import GemYieldTracker
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..plan import OP_STATE, GoalOp, Plan
from ..pathing import op_walk_sets
from ..world import Pos, WorldModel


@dataclass
class PlayContext:
    """Policy, memory, and runtime knobs passed into the state machine (A5)."""

    memory: Memory
    policy: Policy
    rng: random.Random
    never_attack: list[str] = field(default_factory=list)
    params: dict[str, float | int] = field(default_factory=lambda: dict(PARAM_DEFAULTS))
    knowledge: KnowledgeBase | None = None  # per-world door graph, terrain, locked doors (A26, A14)
    plan: Plan | None = None  # validated goal stack (A34)
    directives: Directives = field(default_factory=default_directives)
    gem_cuts: GemYieldTracker | None = None  # cuts waiting out their gem window: exhausted for Gather (A63)
    # The runner's held-queue probe (A64): only the states that can answer
    # with a reflex run (``dispatch.PROBE_STATES``), not the whole list.
    probe: bool = False

    @property
    def tick_hz(self) -> int:
        """The world's tick rate, as the runner learned it onto the plan."""
        return self.plan.tick_hz if self.plan is not None else DEFAULT_TICK_RATE_HZ


@dataclass
class StateOutcome:
    """One picked state's answer for this round trip (M7 test seam)."""

    intents: list[Intent] | None
    reason: str
    reflex: bool = False
    state: str = ""
    # Already paced (A23 **Fight**): the runner sends it as is.
    paced: bool = False
    # Intentional wait (A44): no intent, but the state still holds the round,
    # so dispatch does not fall through to lower states.
    wait: bool = False
    # "State: reason" of each higher state that claimed the round, sent no
    # intent and fell through (A44 diagnostics).
    yielded: list[str] = field(default_factory=list)
    # False when the intents are a try at the op that does not move toward it
    # (a Compose or Use that has not finished it yet): the op's stall clock
    # keeps running, so a refused try cannot pin the stack (A34, A39).
    progress: bool = True


class State(ABC):
    """Priority state: guard, act, done (M7 / A5).

    ``guard`` says whether the state may take over; ``done`` whether the
    active state may let go. The gap between the two is the hysteresis.
    """

    name: str

    @abstractmethod
    def guard(self, world: WorldModel, ctx: PlayContext) -> bool: ...

    @abstractmethod
    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome: ...

    @abstractmethod
    def done(self, world: WorldModel, ctx: PlayContext) -> bool: ...


def top_op(ctx: PlayContext) -> GoalOp | None:
    """The plan's current op, or None with no plan or an empty stack."""
    return ctx.plan.current() if ctx.plan is not None else None


def top_executor(ctx: PlayContext) -> str | None:
    """The name of the state that carries out the top op, or None with no plan op."""
    op = top_op(ctx)
    return OP_STATE.get(op["op"]) if op is not None else None


def my_op(ctx: PlayContext, state: str) -> GoalOp | None:
    """The top op when ``state`` is its executor, else None.

    Every executor's guard asks this: it runs only to carry out the
    planner's current top op (PLAN.md **Architecture**).
    """
    op = top_op(ctx)
    return op if op is not None and OP_STATE.get(op["op"]) == state else None


def op_walk(w: WorldModel, ctx: PlayContext) -> tuple[set[Pos], set[Pos], set[Pos]]:
    """``plan_avoid``, ``plan_costly`` and ``reach`` for a walk on the op's
    behalf (``pathing.op_walk_sets``, A128): every op executor's walk, and the
    way it clears, keeps out of the reach of the hostiles seen that the
    engagement would not fight."""
    return op_walk_sets(w, ctx.memory, ctx.policy, ctx.knowledge, ctx.params, ctx.never_attack)
