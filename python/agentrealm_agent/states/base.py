"""State protocol, the context states run in, and one dispatch round's outcome."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..client import Intent
from ..config import Policy
from ..directives import PARAM_DEFAULTS, Directives, default_directives
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..plan import Plan
from ..world import WorldModel


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
