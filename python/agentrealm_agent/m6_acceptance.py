"""M6 acceptance metrics (A4): live Olympuff smoke test counters."""

from __future__ import annotations

from dataclasses import dataclass, field

TARGET_STEPS = 200
CALM_BUDGET_FRACTION = 0.25


@dataclass
class M6AcceptanceMetrics:
    """Counts windows and steps while the runner drives a character."""

    target_steps: int = TARGET_STEPS
    steps_applied: int = 0
    movement_cooldown_rejections: int = 0
    calm_windows: int = 0
    calm_calls: int = 0  # any API call in a calm window (reads included)
    calm_tick_calls: int = 0  # POST tick only; M6 done-when is poll cadence
    urgent_windows: int = 0
    rejection_codes: list[str] = field(default_factory=list)

    def on_window(self, *, urgent: bool, call: str) -> None:
        spent = call != "skip"
        if urgent:
            self.urgent_windows += 1
        else:
            self.calm_windows += 1
            if spent:
                self.calm_calls += 1
            if call == "tick":
                self.calm_tick_calls += 1

    def on_step_applied(self) -> None:
        self.steps_applied += 1

    def on_rejection(self, code: str) -> None:
        self.rejection_codes.append(code)
        if code == "movement_cooldown":
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
        ]
        if self.rejection_codes:
            other = [c for c in self.rejection_codes if c != "movement_cooldown"]
            if other:
                lines.append(f"other rejections: {', '.join(other)}")
        return lines
