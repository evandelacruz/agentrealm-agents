"""M6 acceptance metrics (A4): live Olympuff smoke test counters."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .client import ApiError

TARGET_STEPS = 200
CALM_BUDGET_FRACTION = 0.25


@dataclass
class M6AcceptanceMetrics:
    """Counts windows, API calls and steps while the runner drives a character.

    Calls are counted on the client itself (``wrap``), so every request a
    window makes is seen, not just the one the scheduler chose. ``stop`` is
    set once ``target_steps`` Steps have applied.
    """

    target_steps: int = TARGET_STEPS
    stop: threading.Event | None = None
    steps_applied: int = 0
    movement_cooldown_rejections: int = 0
    calm_windows: int = 0
    calm_calls: int = 0  # any API call in a calm window (reads included)
    calm_tick_calls: int = 0  # POST tick only; M6 done-when is poll cadence
    urgent_windows: int = 0
    rejection_codes: list[str] = field(default_factory=list)
    # Failed requests ("tick 400 malformed_intent", "zone 429 rate_limited"):
    # an ingest refusal never reaches on_rejection, and each one spent a call.
    api_errors: list[str] = field(default_factory=list)
    window_calls: list[str] = field(default_factory=list)  # this window's calls so far

    def wrap(self, client):
        """``client`` with every method call recorded against the current window."""
        return _CountingClient(client, self.window_calls, self.api_errors)

    def on_window(self, *, urgent: bool) -> None:
        """Close one window: file the calls it made as urgent or calm."""
        calls, self.window_calls[:] = list(self.window_calls), []
        if urgent:
            self.urgent_windows += 1
        else:
            self.calm_windows += 1
            self.calm_calls += len(calls)
            self.calm_tick_calls += calls.count("tick")

    def on_step_applied(self) -> None:
        self.steps_applied += 1
        if self.stop is not None and self.reached_step_goal():
            self.stop.set()

    def on_rejection(self, code: str, *, verb: str | None = None) -> None:
        self.rejection_codes.append(code)
        if code == "movement_cooldown" and verb == "Step":
            self.movement_cooldown_rejections += 1

    @property
    def calm_call_fraction(self) -> float:
        if self.calm_windows == 0:
            return 0.0
        return self.calm_calls / self.calm_windows

    @property
    def calm_tick_fraction(self) -> float:
        if self.calm_windows == 0:
            return 0.0
        return self.calm_tick_calls / self.calm_windows

    def reached_step_goal(self) -> bool:
        return self.steps_applied >= self.target_steps

    def failures(self) -> list[str]:
        out: list[str] = []
        if not self.reached_step_goal():
            out.append(f"steps {self.steps_applied} < {self.target_steps}")
        if self.movement_cooldown_rejections:
            out.append(
                f"{self.movement_cooldown_rejections} movement_cooldown rejection(s)"
            )
        if self.calm_windows and self.calm_tick_fraction >= CALM_BUDGET_FRACTION:
            out.append(
                f"calm tick POSTs {self.calm_tick_fraction:.1%} "
                f"({self.calm_tick_calls}/{self.calm_windows} windows), "
                f"need under {CALM_BUDGET_FRACTION:.0%}"
            )
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self) -> list[str]:
        lines = [
            f"steps applied: {self.steps_applied} (target {self.target_steps})",
            f"movement_cooldown rejections: {self.movement_cooldown_rejections}",
            f"calm windows: {self.calm_windows}, calm tick POSTs: {self.calm_tick_calls} "
            f"({self.calm_tick_fraction:.1%} of calm windows)",
            f"calm API calls (reads included): {self.calm_calls} "
            f"({self.calm_call_fraction:.1%} of calm windows)",
            f"urgent windows: {self.urgent_windows}",
            f"API errors: {len(self.api_errors)}",
        ]
        if self.rejection_codes:
            other = [c for c in self.rejection_codes if c != "movement_cooldown"]
            if other:
                lines.append(f"other rejections: {', '.join(other)}")
        return lines


class _CountingClient:
    """Forwards to a client, appending each method name called to ``calls``
    and each failed request to ``errors``."""

    def __init__(self, inner, calls: list[str], errors: list[str]):
        self._inner = inner
        self._calls = calls
        self._errors = errors

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self._calls.append(name)  # a request is spent even if it fails
            try:
                return attr(*args, **kwargs)
            except ApiError as e:
                self._errors.append(f"{name} {e.status} {e.code}")
                raise

        return call
