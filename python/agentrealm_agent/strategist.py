"""Background strategist thread with optional LLM (A35).

Runs off the tick loop. The runner keeps the current plan until a validated
strategist answer lands, then replaces the goal stack (unless directives
``goals`` own the stack). With no model configured, signals are drained and
the built-in plan plus no-LLM clue rules (A32) stay in charge.

Configure with ``AGENTREALM_STRATEGIST_MODEL`` and
``AGENTREALM_STRATEGIST_API_KEY`` (or ``OPENAI_API_KEY``). Optional limits:
``AGENTREALM_STRATEGIST_MIN_INTERVAL_S``, ``AGENTREALM_STRATEGIST_MAX_CALLS``,
``AGENTREALM_STRATEGIST_MAX_USD``, ``AGENTREALM_STRATEGIST_IDLE_MINUTES``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .directives import Directives
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .plan import GoalOp, Plan, is_travel_goal, parse_directives_goals, parse_plan_payload
from .world import WorldModel

log = logging.getLogger(__name__)

SIGNALS_KEPT = 16
DEFAULT_MIN_INTERVAL_S = 60.0
DEFAULT_MAX_CALLS = 48
DEFAULT_MAX_USD = 2.0
DEFAULT_IDLE_MINUTES = 10
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """You are the strategist for an Agent Realm character. Reply with one JSON object only, no markdown, with exactly these keys:
- "goals": array of plan operations (see schema below)
- "params": optional survival params object
- "notes": optional string for the trace

Each goal is an object with "op" and the fields for that op. Valid ops include travel, explore_area, read, say, buy, break_block, use_block, compose, fetch_item, gather_gems, hunt, enter_level, fight_boss, avoid, wait, set_param. Unknown ops are dropped.

Example:
{"goals":[{"op":"buy","code":"torch","why":"clue mentions darkness"}],"params":{"curiosity":0.3},"notes":"try the cave entrance"}
"""


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]: ...


@dataclass
class StrategistConfig:
    model: str = ""
    api_key: str = ""
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    max_calls: int = DEFAULT_MAX_CALLS
    max_usd: float = DEFAULT_MAX_USD
    idle_ticks: int = DEFAULT_IDLE_MINUTES * 600  # 10 Hz default

    @property
    def enabled(self) -> bool:
        return bool(self.model and self.api_key)

    @classmethod
    def from_env(cls, *, tick_hz: int = 10) -> StrategistConfig:
        model = os.environ.get("AGENTREALM_STRATEGIST_MODEL", "").strip()
        api_key = (
            os.environ.get("AGENTREALM_STRATEGIST_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                log.warning("strategist: ignoring bad %s=%r", name, raw)
                return default

        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                log.warning("strategist: ignoring bad %s=%r", name, raw)
                return default

        idle_min = _float("AGENTREALM_STRATEGIST_IDLE_MINUTES", DEFAULT_IDLE_MINUTES)
        return cls(
            model=model,
            api_key=api_key,
            min_interval_s=_float("AGENTREALM_STRATEGIST_MIN_INTERVAL_S", DEFAULT_MIN_INTERVAL_S),
            max_calls=_int("AGENTREALM_STRATEGIST_MAX_CALLS", DEFAULT_MAX_CALLS),
            max_usd=_float("AGENTREALM_STRATEGIST_MAX_USD", DEFAULT_MAX_USD),
            idle_ticks=max(1, int(idle_min * 60 * tick_hz)),
        )


def queue_signal(m: Memory, payload: dict[str, Any]) -> None:
    """Append one strategist trigger; keep the last ``SIGNALS_KEPT``."""
    m.strategist_signals.append(payload)
    del m.strategist_signals[:-SIGNALS_KEPT]


def note_goal_done(m: Memory | None, op: GoalOp | None, reason: str) -> None:
    if m is None or op is None:
        return
    queue_signal(m, {"trigger": "goal_done", "op": dict(op), "reason": reason})


def note_goal_failed(m: Memory | None, op: GoalOp | None, reason: str) -> None:
    if m is None or op is None:
        return
    queue_signal(m, {"trigger": "goal_failed", "op": dict(op), "reason": reason})


def drain_triggers(m: Memory) -> list[dict[str, Any]]:
    """Take every queued trigger (clue, stuck, and strategist signals)."""
    out: list[dict[str, Any]] = []
    out.extend(m.clue_signals)
    m.clue_signals.clear()
    out.extend(m.nav_stuck.stuck_signals)
    m.nav_stuck.stuck_signals.clear()
    out.extend(m.strategist_signals)
    m.strategist_signals.clear()
    return out


class OpenAIChatClient:
    """Minimal OpenAI chat client (stdlib only)."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            OPENAI_CHAT_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"openai http {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"openai network: {e}") from e
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("openai: empty choices")
        content = choices[0].get("message", {}).get("content", "")
        if not isinstance(content, str):
            raise RuntimeError("openai: missing message content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return content.strip(), usage


def _estimate_usd(usage: dict[str, Any]) -> float:
    """Rough cost from token counts when the response omits a price."""
    try:
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
    except (TypeError, ValueError):
        return 0.0
    # Placeholder rates; trace shows tokens even when this is zero.
    return (prompt * 0.15 + completion * 0.6) / 1_000_000.0


def build_prompt(
    *,
    triggers: list[dict[str, Any]],
    w: WorldModel,
    plan: Plan,
    directives: Directives,
    knowledge: KnowledgeBase | None,
) -> list[dict[str, str]]:
    pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos and w.map_id is not None else "unknown"
    state_lines = [
        f"tick={w.tick} pos={pos} alive={w.alive} health={w.health}/{w.max_health} gems={w.gems}",
        f"map_level={w.map_level} armed={w.armed_code} lives={w.lives}",
    ]
    head = plan.current()
    if head is not None:
        state_lines.append(f"plan_head={json.dumps(head, sort_keys=True)}")
    if plan.notes:
        state_lines.append(f"plan_notes={plan.notes!r}")
    clues: list[dict[str, Any]] = []
    if knowledge is not None:
        with knowledge.lock:
            clues = list(knowledge.clues)[-12:]
    user_parts = [
        "Triggers:\n" + json.dumps(triggers, sort_keys=True),
        "State:\n" + "\n".join(state_lines),
        "Clues (newest last):\n" + json.dumps(clues, sort_keys=True),
        "Directives instructions:\n" + (directives.instructions or "(none)"),
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


@dataclass
class Strategist:
    """Async strategist: drains triggers and optionally calls a model."""

    config: StrategistConfig
    lock: threading.Lock
    _client: LLMClient | None = None
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)
    _runner_getter: Callable[[], Any] | None = field(default=None, repr=False)
    _log: Callable[[str, str, dict], None] | None = field(default=None, repr=False)
    _pending: tuple[list[GoalOp], dict[str, float | int], str] | None = field(default=None, repr=False)
    _calls: int = 0
    _spent_usd: float = 0.0
    _last_call_at: float = 0.0
    _last_level_key: tuple[int, int] | None = field(default=None, repr=False)
    _idle_sent_for_tick: int = -1

    @classmethod
    def from_env(cls, lock: threading.Lock, *, tick_hz: int = 10) -> Strategist:
        cfg = StrategistConfig.from_env(tick_hz=tick_hz)
        client = OpenAIChatClient(cfg.api_key, cfg.model) if cfg.enabled else None
        return cls(config=cfg, lock=lock, _client=client)

    def start(self, runner: Any) -> None:
        self._runner_getter = lambda: runner
        self._log = runner.log
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="strategist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def notify(self) -> None:
        self._wake.set()

    def apply_pending(self, runner: Any) -> bool:
        """Replace the runner plan when a strategist answer is ready."""
        pending = self._pending
        if pending is None:
            return False
        if _operator_plan_active(runner.directives.directives):
            self._pending = None
            return False
        goals, params, notes = pending
        self._pending = None
        d = runner.directives.directives
        runner.plan = Plan(
            list(goals),
            dict(params),
            notes=notes,
            floor_params=dict(d.params),
            tick_hz=runner.tick_hz,
        )
        runner.mem.path, runner.mem.goal, runner.mem.goal_op = [], "", None
        if self._log:
            self._log(
                "strategist",
                f"plan replaced ({len(goals)} goals)",
                {"strategist": {"event": "applied", "goals": goals, "params": params, "notes": notes}},
            )
        return True

    def track_window(self, runner: Any) -> None:
        """Queue level and idle triggers; nudge the background thread."""
        w, m = runner.world, runner.mem
        if w.map_id is not None and w.map_level is not None and w.map_level > 0:
            key = (w.map_id, w.map_level)
            if key != self._last_level_key:
                self._last_level_key = key
                queue_signal(
                    m,
                    {"trigger": "level", "map_id": w.map_id, "level": w.map_level, "tick": w.tick},
                )
                self.notify()
        progress_tick = m.strategist_progress_tick
        if progress_tick >= 0 and w.tick - progress_tick >= self.config.idle_ticks:
            if self._idle_sent_for_tick != progress_tick:
                self._idle_sent_for_tick = progress_tick
                queue_signal(
                    m,
                    {
                        "trigger": "idle",
                        "since_tick": progress_tick,
                        "tick": w.tick,
                        "idle_ticks": self.config.idle_ticks,
                    },
                )
                self.notify()

    def note_progress(self, m: Memory, tick: int) -> None:
        m.strategist_progress_tick = tick

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            runner = self._runner_getter() if self._runner_getter else None
            if runner is None:
                continue
            with self.lock:
                triggers = drain_triggers(runner.mem)
            if not triggers:
                continue
            if not self.config.enabled:
                if self._log:
                    self._log(
                        "strategist",
                        f"drained {len(triggers)} trigger(s), no model",
                        {"strategist": {"event": "drain", "triggers": triggers}},
                    )
                continue
            if not self._rate_ok():
                # Put triggers back so a later window can use them.
                with self.lock:
                    for t in triggers:
                        _requeue(runner.mem, t)
                continue
            try:
                with self.lock:
                    messages = build_prompt(
                        triggers=triggers,
                        w=runner.world,
                        plan=runner.plan,
                        directives=runner.directives.directives,
                        knowledge=runner.knowledge,
                    )
                assert self._client is not None
                raw_text, usage = self._client.complete(messages)
                raw_obj = json.loads(raw_text)
                floor = dict(runner.directives.directives.params)
                goals, params, notes = parse_plan_payload(raw_obj, floor_params=floor)
                cost = _estimate_usd(usage)
                self._calls += 1
                self._spent_usd += cost
                self._last_call_at = time.time()
                with self.lock:
                    self._pending = (goals, params, notes)
                if self._log:
                    self._log(
                        "strategist",
                        f"answer ({len(goals)} goals, call {self._calls})",
                        {
                            "strategist": {
                                "event": "answer",
                                "triggers": triggers,
                                "raw": raw_text,
                                "goals": goals,
                                "params": params,
                                "notes": notes,
                                "usage": usage,
                                "spent_usd": round(self._spent_usd, 6),
                            }
                        },
                    )
            except Exception as e:
                log.warning("strategist: call failed: %s", e)
                if self._log:
                    self._log(
                        "strategist",
                        f"failed: {e}",
                        {"strategist": {"event": "error", "triggers": triggers, "error": str(e)}},
                    )

    def _rate_ok(self) -> bool:
        if self._calls >= self.config.max_calls:
            return False
        if self._spent_usd >= self.config.max_usd:
            return False
        if self._last_call_at and time.time() - self._last_call_at < self.config.min_interval_s:
            return False
        return True


def _operator_plan_active(directives: Directives) -> bool:
    stack = [g for g in directives.goals if not is_travel_goal(g)]
    if not stack:
        return False
    return bool(parse_directives_goals(stack))


def _requeue(m: Memory, trigger: dict[str, Any]) -> None:
    kind = trigger.get("trigger")
    if kind == "clue":
        m.clue_signals.append(trigger)
        del m.clue_signals[:-SIGNALS_KEPT]
    elif kind == "stuck":
        m.nav_stuck.stuck_signals.append(trigger)
        del m.nav_stuck.stuck_signals[:-SIGNALS_KEPT]
    else:
        queue_signal(m, trigger)
