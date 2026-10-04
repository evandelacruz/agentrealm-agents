"""State protocol and the outcome of one dispatch round."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..client import Intent
from ..world import WorldModel
from ..brain import PlayContext


@dataclass
class StateOutcome:
    """One picked state's answer for this round trip (M7 test seam)."""

    intents: list[Intent] | None
    reason: str
    reflex: bool = False
    state: str = ""


class State(ABC):
    """Priority state: guard, act, done (M7 / A5)."""

    name: str

    @abstractmethod
    def guard(self, world: WorldModel, ctx: PlayContext) -> bool: ...

    @abstractmethod
    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome: ...

    @abstractmethod
    def done(self, world: WorldModel, ctx: PlayContext) -> bool: ...
