"""The hooks a live acceptance run (M6 A4, M7 A16) gets from the runner.

The runner calls every hook unconditionally whenever an acceptance object is
attached. Each one does nothing here, so a metrics class overrides only the
hooks it measures.
"""

from __future__ import annotations

from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import WorldModel


class AcceptanceHooks:
    """No-op base for acceptance metrics. Override the hooks you need."""

    def wrap(self, client):
        """The client the runner should call through (to count requests or errors)."""
        return client

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        """One sim window has ended, whether or not it sent anything."""

    def before_tick(
        self,
        w: WorldModel,
        m: Memory,
        *,
        state: str,
        reason: str,
        intents: list[dict] | None,
        policy: Policy,
        params: dict[str, float | int],
        knowledge: KnowledgeBase | None,
    ) -> None:
        """A tick is about to be sent. ``w`` is the world the decision saw.

        ``intents`` is None when a held queue keeps running and nothing new is sent.
        """

    def on_step_applied(self) -> None:
        """One of our Steps applied."""

    def on_rejection(self, code: str, *, verb: str | None = None) -> None:
        """One of our intents was rejected with ``code``."""

    def on_death(self) -> None:
        """A ``Died`` event arrived for our character."""
