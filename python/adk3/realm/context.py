"""The context every need, skill, engine and opportunity receives: the moment and what is known."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.world import Pos

from .memory import Memory
from .state import State


@dataclass
class Ctx:
    state: State
    memory: Memory
    kb: KnowledgeBase | None  # tiles and door warps, for travel
    nav: Any  # the ADK's navigation memory: what rejections taught the map
    town: tuple[int, Pos] | None  # the town's map and one block in it (GetWorld)
    judgments: dict[str, Any] = field(default_factory=dict)  # the strategist's, by need key
    tactics: dict[str, Any] = field(default_factory=dict)  # the strategist's, by engine name

    @property
    def s(self) -> State:
        return self.state
