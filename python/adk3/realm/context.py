"""The context every skill, reaction and opportunity receives: the moment and what is known."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.world import Pos

from .memory import Memory
from .state import State
from .value import Levers


@dataclass
class Ctx:
    state: State
    memory: Memory
    kb: KnowledgeBase | None  # tiles and door warps, for travel
    nav: Any  # the ADK's navigation memory: what rejections taught the map
    town: tuple[int, Pos] | None  # the town's map and one block in it (GetWorld)
    levers: Levers = field(default_factory=Levers)  # the agent's temperament, from its profile
    tactics: dict[str, Any] = field(default_factory=dict)  # the running approach's, for the reactions
    approach: Any = None  # the running approach

    @property
    def s(self) -> State:
        return self.state
