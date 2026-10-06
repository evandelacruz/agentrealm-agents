"""What the acceptance runs (M6 A4, M7 A16, M8 A25, M9 A29, M10 A33, M4 A36, M11 A40) share.

``AcceptanceHooks`` are the hooks the runner calls on an attached acceptance
object; each does nothing here, so a metrics class overrides only the hooks it
measures; M8 also reads tick events (``NPCDied``) through ``on_events``, and
M11 level clears through ``on_level_clear``. A new hook is declared here and
called unconditionally, never looked up with ``hasattr``. ``CountingClient``
records failed requests (and optionally every call).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

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
        acted_op: dict | None = None,
        plan_op: dict | None = None,
    ) -> None:
        """A tick is about to be sent. ``w`` is the world the decision saw.

        ``intents`` is None when a held queue keeps running and nothing new is sent.
        ``plan_op`` is the plan stack's head op (``Plan.current()``), or None (A40).
        ``acted_op`` is the goal-stack op this decision acted on (``Plan.acted``):
        set only when the state that owns the head op stepped toward it or sent
        its Take or Use, None otherwise (a reflex, another goal, a held queue) (A36).
        It is not the stack's head op, which a gate that needs it reads as its
        own kwarg (``plan_op``, ``Plan.current()``).
        """

    def on_planner(self, *, enabled: bool) -> None:
        """The runner starts, with the AI planner on or off (``--no-planner``)."""

    def on_strategist_reply(self) -> None:
        """The planner answered with a plan: at least one valid op, or an explicit empty stack."""

    def on_strategist_error(self, error: str, *, auth: bool = False) -> None:
        """A planner call failed (network, HTTP status, refusal), or its reply
        was not a JSON object or held only invalid ops.

        ``auth`` is True when the provider refused the key (HTTP 401 or 403)."""

    def on_strategist_trigger(self, trigger: dict) -> None:
        """A trigger (``clue``, ``goal_done``, …) moved into the strategist's inbox (A36)."""

    def on_strategist_applied(self, goals: list[dict]) -> None:
        """The strategist replaced the goal stack with ``goals`` (A36)."""

    def on_step_applied(self) -> None:
        """One of our Steps applied."""

    def on_rejection(self, code: str, *, verb: str | None = None) -> None:
        """One of our intents was rejected with ``code``."""

    def on_death(self) -> None:
        """A ``Died`` event arrived for our character."""

    def on_events(self, events: list[dict]) -> None:
        """Tick events after the response is applied (for example ``NPCDied``)."""

    def on_level_clear(self, ceremony: dict) -> None:
        """A round trip carried a one-shot ``level_clear_ceremony`` (A38, A40)."""

    def on_oscillation(self, event: dict) -> None:
        """The dispatch guard caught the character pacing between two cells (A15)."""


# Planner failures in a row (no accepted plan between) that fail a run. Fewer
# is a transient provider error (an overload, a timeout) the backoff recovered from.
PLANNER_ERROR_STREAK = 3


@dataclass(kw_only=True)
class PlannerHealth(AcceptanceHooks):
    """Planner errors and accepted plans, for every live run with the planner on.

    A run fails on ``PLANNER_ERROR_STREAK`` failures in a row, on any refused
    key (401 or 403), or when the planner was on and never got one plan
    accepted: a planner that only fails is not playing. A lone error the
    backoff recovers from is counted and reported, not a failure.
    """

    planner_on: bool = False
    planner_errors: int = 0  # every failure this run
    plans_accepted: int = 0
    auth_errors: int = 0
    error_streak: int = 0  # failures since the last accepted plan
    longest_error_streak: int = 0

    def on_planner(self, *, enabled: bool) -> None:
        self.planner_on = enabled

    def on_strategist_reply(self) -> None:
        self.plans_accepted += 1
        self.error_streak = 0

    def on_strategist_error(self, error: str, *, auth: bool = False) -> None:
        self.planner_errors += 1
        self.auth_errors += auth
        self.error_streak += 1
        self.longest_error_streak = max(self.longest_error_streak, self.error_streak)

    def planner_failures(self) -> list[str]:
        out: list[str] = []
        if self.auth_errors:
            out.append(f"{self.auth_errors} planner auth error(s) (401/403)")
        if self.longest_error_streak >= PLANNER_ERROR_STREAK:
            out.append(f"{self.longest_error_streak} planner errors in a row ({self.planner_errors} in all)")
        if self.planner_on and not self.plans_accepted:
            out.append("planner on but no plan accepted")
        return out

    def planner_summary_line(self) -> str:
        if not self.planner_on:
            return "planner: off (--no-planner)"
        return f"planner: {self.plans_accepted} plan(s) accepted, {self.planner_errors} error(s)"


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
