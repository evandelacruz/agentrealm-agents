"""M7 acceptance metrics (A16): live Olympuff survival and navigation counters."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .client import ApiError
from .config import Policy
from .directives import PARAM_DEFAULTS
from .memory import Memory
from .healing import regen_known
from .knowledge_base import KnowledgeBase
from .survival import should_retreat
from .world import WorldModel, chebyshev

TARGET_SECONDS = 3600.0
TARGET_DISTANCE = 150
LOOP_SAME_REASON_LIMIT = 24  # consecutive ticks at one cell with one reason → loop


@dataclass
class M7AcceptanceMetrics:
    """Counts survival, navigation and API faults while the runner drives M7.

    ``stop`` is set once ``target_seconds`` of wall clock have elapsed and the
    character is still alive on the overworld (or when ``fail_fast`` triggers).
    """

    target_seconds: float = TARGET_SECONDS
    target_distance: int = TARGET_DISTANCE
    stop: threading.Event | None = None
    started_monotonic: float | None = None
    overworld_map_id: int | None = None
    origin: tuple[int, int] | None = None
    max_distance: int = 0
    deaths: int = 0
    lives_seen: int | None = None
    retreat_misses: int = 0  # should_retreat held while state was not survival
    recover_withdraws: int = 0
    recover_unsafe: int = 0
    heal_actions: int = 0
    regen_measured: bool = False
    give_up_reasons: list[str] = field(default_factory=list)
    api_errors: list[str] = field(default_factory=list)
    window_calls: list[str] = field(default_factory=list)
    calm_windows: int = 0
    calm_tick_calls: int = 0
    _loop_pos: tuple[int, int] | None = None
    _loop_reason: str = ""
    _loop_streak: int = 0
    loop_detected: bool = False

    def wrap(self, client):
        return _CountingClient(client, self.window_calls, self.api_errors)

    def on_window(self, *, urgent: bool) -> None:
        calls, self.window_calls[:] = list(self.window_calls), []
        if not urgent:
            self.calm_windows += 1
            self.calm_tick_calls += calls.count("tick")

    def on_rejection(self, code: str, *, verb: str | None = None) -> None:
        del code, verb

    def note_overworld(self, map_id: int | None) -> None:
        if map_id is not None:
            self.overworld_map_id = map_id

    def on_position(self, w: WorldModel) -> None:
        if w.map_id is None or w.pos is None:
            return
        if self.overworld_map_id is not None and w.map_id != self.overworld_map_id:
            return
        if self.origin is None:
            self.origin = w.pos
        self.max_distance = max(self.max_distance, chebyshev(w.pos, self.origin))

    def on_decision(
        self,
        w: WorldModel,
        m: Memory,
        *,
        reason: str,
        state: str,
        policy: Policy,
        params: dict[str, float | int],
        intents: list[dict] | None,
        knowledge: KnowledgeBase | None = None,
    ) -> None:
        self.on_position(w)
        if w.lives is not None:
            self.lives_seen = w.lives
        if state == "Heal" and intents:
            self.heal_actions += 1
        if state == "Recover" and intents:
            self.recover_withdraws += 1
            from .states.recover import recover_spot_safe

            chest = w.death_chest
            if chest is not None and not recover_spot_safe(w, chest[0], chest[1]):
                self.recover_unsafe += 1
        if should_retreat(w, policy, params) and state not in (
            "Retreat",
            "Flee",
            "Escape",
            "Downed",
            "Sync",
            "Heal",
        ):
            self.retreat_misses += 1
        if m.nav_stuck.stuck_signals:
            for sig in m.nav_stuck.stuck_signals:
                r = sig.get("escalation") or sig.get("reason") or ""
                if r and r not in self.give_up_reasons:
                    self.give_up_reasons.append(str(r))
        if regen_known(knowledge, m) == "yes":
            self.regen_measured = True
        if w.pos is not None and w.map_id == self.overworld_map_id:
            key = (w.map_id, w.pos)
            if key == self._loop_pos and reason == self._loop_reason:
                self._loop_streak += 1
                if self._loop_streak >= LOOP_SAME_REASON_LIMIT:
                    self.loop_detected = True
            else:
                self._loop_pos, self._loop_reason = key, reason
                self._loop_streak = 1

    def on_death(self) -> None:
        self.deaths += 1

    def on_time(self, elapsed: float, *, alive: bool) -> None:
        if elapsed >= self.target_seconds and alive and self.stop is not None:
            self.stop.set()

    def navigation_ok(self) -> bool:
        if self.max_distance >= self.target_distance:
            return True
        return bool(self.give_up_reasons)

    def failures(self) -> list[str]:
        out: list[str]
        out = []
        if self.deaths:
            out.append(f"{self.deaths} death(s) during run")
        if self.retreat_misses:
            out.append(f"{self.retreat_misses} tick(s) should_retreat while not in Retreat/Flee/Heal")
        if self.recover_unsafe:
            out.append(f"{self.recover_unsafe} Recover withdraw(s) with unsafe chest spot")
        if self.loop_detected:
            out.append("navigation loop detected (same reason at same cell)")
        if not self.navigation_ok():
            out.append(
                f"max distance {self.max_distance} < {self.target_distance} and no give-up reason recorded"
            )
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self) -> list[str]:
        lines = [
            f"max chebyshev distance from origin: {self.max_distance} (target {self.target_distance} or give-up)",
            f"deaths: {self.deaths}",
            f"retreat misses: {self.retreat_misses}",
            f"recover withdraws: {self.recover_withdraws} (unsafe {self.recover_unsafe})",
            f"heal actions: {self.heal_actions}",
            f"regen measured: {self.regen_measured}",
            f"give-up reasons: {', '.join(self.give_up_reasons) or 'none'}",
            f"loop detected: {self.loop_detected}",
            f"API errors: {len(self.api_errors)}",
            f"lives last seen: {self.lives_seen}",
        ]
        return lines


class _CountingClient:
    def __init__(self, inner, calls: list[str], errors: list[str]):
        self._inner = inner
        self._calls = calls
        self._errors = errors

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self._calls.append(name)
            try:
                return attr(*args, **kwargs)
            except ApiError as e:
                self._errors.append(f"{name} {e.status} {e.code}")
                raise

        return call


def params_with_defaults(raw: dict[str, float | int] | None) -> dict[str, float | int]:
    out = dict(PARAM_DEFAULTS)
    if raw:
        out.update(raw)
    return out
