"""What the live acceptance runs (M6 A4, M7 A16, M8 A25, M10 A33) share.

``AcceptanceHooks`` are the hooks the runner calls on an attached acceptance
object; each does nothing here, so a metrics class overrides only the hooks it
measures; M8 also reads tick events (``NPCDied``) through ``on_events``.
``CountingClient`` records failed requests (and optionally every call).
"""

from __future__ import annotations

import threading

from .config import Policy
from .client import ApiError
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import WorldModel


class AcceptanceHooks:
    """No-op base for acceptance metrics. Override the hooks you need.

    ``stop``, when set, is the runner's stop event: a metrics class sets it to
    end the run early (clock done, death, a goal reached).
    """

    stop: threading.Event | None = None

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

    def on_events(self, events: list[dict]) -> None:
        """Tick events after the response is applied (for example ``NPCDied``)."""

    def on_oscillation(self, event: dict) -> None:
        """The dispatch guard caught the character pacing between two cells (A15)."""


class CountingClient:
    """Forwards to a client, appending each failed request to ``errors`` and,
    when ``calls`` is given, each method name called to it."""

    def __init__(self, inner, errors: list[str], calls: list[str] | None = None):
        self._inner = inner
        self._errors = errors
        self._calls = calls

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if self._calls is not None:
                self._calls.append(name)  # a request is spent even if it fails
            try:
                return attr(*args, **kwargs)
            except ApiError as e:
                self._errors.append(f"{name} {e.status} {e.code}")
                raise

        return call
