"""Strategist: an optional LLM that rewrites the goal stack (A35).

Off unless ``AGENTREALM_STRATEGIST_MODEL`` and an API key are set. With it
off, triggers are drained and logged, and the built-in plan plus the no-LLM
clue rules (A32) stay in charge.

Two threads, one rule: everything that reads or writes the world, the plan,
``Memory`` or the trace runs on the tick thread, in :meth:`Strategist.on_window`.
The background thread only sends a prompt and waits for the reply. The two
threads talk through two queues, a request and an answer, so a slow model
never costs a tick and nothing needs a lock.

One window, on the tick thread:

1. Move new triggers (clue, stuck, death, goal done or failed, level, idle)
   from ``Memory`` into the strategist's inbox.
2. If an answer came back, apply it. A failed call or a reply that is not
   JSON puts its triggers back in the inbox. Its ``params`` merge in at once; its
   ``goals`` replace the stack only when at least one is valid and directives
   ``goals`` do not own the stack.
3. If nothing is in flight, the inbox has triggers and the limits allow it,
   build the prompt, log it to the trace, and hand it to the background thread.

Limits (environment variables, defaults in brackets). Every attempt counts,
failed ones included:

- ``AGENTREALM_STRATEGIST_MIN_INTERVAL_S`` [60]: seconds between calls.
- ``AGENTREALM_STRATEGIST_MAX_CALLS`` [48]: calls per run.
- ``AGENTREALM_STRATEGIST_MAX_TOKENS`` [200000]: prompt plus answer tokens
  per run, as the API reports them. A call that reports no usage is charged
  an estimate of its prompt (4 characters a token). Tokens, not dollars, so
  the cap holds whatever the model costs: multiply by your model's price.
- ``AGENTREALM_STRATEGIST_IDLE_MINUTES`` [10]: no applied Step for this long
  raises the ``idle`` trigger.

The client speaks the OpenAI chat completions API (stdlib ``urllib``), with
the key from ``AGENTREALM_STRATEGIST_API_KEY`` or ``OPENAI_API_KEY``. Another
provider is a class with the same ``complete`` method (``LLMClient``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .directives import Directives
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .plan import Plan, directive_stack_ops, parse_plan_payload
from .world import WorldModel

log = logging.getLogger(__name__)

INBOX_KEPT = 48  # newest triggers kept while waiting for a call
CHARS_PER_TOKEN = 4  # estimate for a call that reports no usage
DEFAULT_MIN_INTERVAL_S = 60.0
DEFAULT_MAX_CALLS = 48
DEFAULT_MAX_TOKENS = 200_000
DEFAULT_IDLE_MINUTES = 10
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """You are the strategist for an Agent Realm character. Reply with one JSON object only, no markdown, with exactly these keys:
- "goals": array of plan operations (see schema below)
- "params": the full survival params object, every key, starting from the current values under State; omit it to keep them
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
    max_tokens: int = DEFAULT_MAX_TOKENS
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

        def number(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                log.warning("strategist: ignoring bad %s=%r", name, raw)
                return default

        idle_min = number("AGENTREALM_STRATEGIST_IDLE_MINUTES", DEFAULT_IDLE_MINUTES)
        return cls(
            model=model,
            api_key=api_key,
            min_interval_s=number("AGENTREALM_STRATEGIST_MIN_INTERVAL_S", DEFAULT_MIN_INTERVAL_S),
            max_calls=int(number("AGENTREALM_STRATEGIST_MAX_CALLS", DEFAULT_MAX_CALLS)),
            max_tokens=int(number("AGENTREALM_STRATEGIST_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
            idle_ticks=max(1, int(idle_min * 60 * tick_hz)),
        )


def drain_triggers(m: Memory) -> list[dict[str, Any]]:
    """Take every queued trigger (clue, stuck, and strategist signals). Tick thread only."""
    out = m.clue_signals + m.nav_stuck.stuck_signals + m.strategist_signals
    m.clue_signals, m.nav_stuck.stuck_signals, m.strategist_signals = [], [], []
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


def tokens_used(usage: dict[str, Any]) -> int | None:
    """Prompt plus answer tokens the API reported, or None when it reported none."""
    try:
        total = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
    except (TypeError, ValueError):
        return None
    return total or None


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    return sum(len(msg["content"]) for msg in messages) // CHARS_PER_TOKEN + 1


def build_prompt(
    *,
    triggers: list[dict[str, Any]],
    w: WorldModel,
    plan: Plan,
    directives: Directives,
    knowledge: KnowledgeBase | None,
) -> list[dict[str, str]]:
    """The model's input: triggers, state, the remaining plan, every clue, and instructions."""
    pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos and w.map_id is not None else "unknown"
    state_lines = [
        f"tick={w.tick} pos={pos} alive={w.alive} health={w.health}/{w.max_health} gems={w.gems}",
        f"map_level={w.map_level} armed={w.armed_code} lives={w.lives}",
        f"params={json.dumps(plan.params, sort_keys=True)}",
        f"params_floor={json.dumps(directives.params, sort_keys=True)} (survival params may only tighten past these)",
    ]
    if plan.notes:
        state_lines.append(f"plan_notes={plan.notes!r}")
    clues: list[dict[str, Any]] = []
    if knowledge is not None:
        with knowledge.lock:
            clues = list(knowledge.clues)
    user_parts = [
        "Triggers:\n" + json.dumps(triggers, sort_keys=True),
        "State:\n" + "\n".join(state_lines),
        "Current plan (top first):\n" + json.dumps(plan.goals[plan.index :], sort_keys=True),
        "Clues (oldest first):\n" + json.dumps(clues, sort_keys=True),
        "Directives instructions:\n" + (directives.instructions or "(none)"),
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


@dataclass
class Answer:
    """What the background thread hands back for one call."""

    raw: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""  # set when the call failed


@dataclass
class Strategist:
    """Asks the model for a new goal stack when a trigger fires (see module docstring)."""

    config: StrategistConfig
    client: LLMClient | None = None
    clock: Callable[[], float] = time.monotonic
    inbox: list[dict[str, Any]] = field(default_factory=list)  # triggers not yet sent
    in_flight: list[dict[str, Any]] | None = None  # triggers of the call being answered
    calls: int = 0
    tokens: int = 0
    last_call_at: float | None = None
    _charged: int = 0  # tokens charged up front for the call in flight
    _last_level_key: tuple[int, int] | None = None
    _idle_sent_for_tick: int = -1
    _requests: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1), repr=False)
    _answers: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, *, tick_hz: int = 10) -> Strategist:
        cfg = StrategistConfig.from_env(tick_hz=tick_hz)
        client = OpenAIChatClient(cfg.api_key, cfg.model) if cfg.enabled else None
        return cls(config=cfg, client=client)

    # --- background thread: send the prompt, parse the reply ---

    def start(self) -> None:
        """Start the background thread; with no model there is nothing to start."""
        if not self.config.enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="strategist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread. A call still running is abandoned; its answer is never applied."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.serve_one(timeout=1.0)

    def serve_one(self, timeout: float | None = None) -> None:
        """Answer one waiting request, if any. Touches nothing but the two queues."""
        try:
            messages = self._requests.get(timeout=timeout)
        except queue.Empty:
            return
        self._answers.put(self._ask(messages))

    def _ask(self, messages: list[dict[str, str]]) -> Answer:
        assert self.client is not None
        answer = Answer()
        try:
            answer.raw, answer.usage = self.client.complete(messages)
        except Exception as e:  # network or HTTP status
            answer.error = str(e) or type(e).__name__
        return answer

    # --- tick thread: everything else ---

    def on_window(self, runner: Any) -> None:
        """Collect triggers, apply an answer that came back, maybe send the next call."""
        self._collect(runner)
        try:
            answer = self._answers.get_nowait()
        except queue.Empty:
            answer = None
        if answer is not None:
            self._settle(runner, answer)
        if not self.inbox:
            return
        if not self.config.enabled:
            runner.log(
                "strategist",
                f"drained {len(self.inbox)} trigger(s), no model",
                {"strategist": {"event": "drain", "triggers": self.inbox}},
            )
            self.inbox = []
            return
        if self.in_flight is None and not self.limit_reached():
            self._send(runner)

    def limit_reached(self) -> str:
        """Why no call may start now, or "" when one may."""
        if self.calls >= self.config.max_calls:
            return "max_calls"
        if self.tokens >= self.config.max_tokens:
            return "max_tokens"
        if self.last_call_at is not None and self.clock() - self.last_call_at < self.config.min_interval_s:
            return "min_interval"
        return ""

    def _collect(self, runner: Any) -> None:
        """Move queued signals into the inbox and raise the level and idle triggers."""
        w, m = runner.world, runner.mem
        if w.map_id is not None and w.map_level is not None and w.map_level > 0:
            key = (w.map_id, w.map_level)
            if key != self._last_level_key:
                self._last_level_key = key
                self.inbox.append({"trigger": "level", "map_id": w.map_id, "level": w.map_level, "tick": w.tick})
        since = m.strategist_progress_tick
        if since >= 0 and w.tick - since >= self.config.idle_ticks and self._idle_sent_for_tick != since:
            self._idle_sent_for_tick = since
            self.inbox.append(
                {"trigger": "idle", "since_tick": since, "tick": w.tick, "idle_ticks": self.config.idle_ticks}
            )
        self.inbox.extend(drain_triggers(m))
        del self.inbox[:-INBOX_KEPT]

    def _send(self, runner: Any) -> None:
        messages = build_prompt(
            triggers=self.inbox,
            w=runner.world,
            plan=runner.plan,
            directives=runner.directives.directives,
            knowledge=runner.knowledge,
        )
        # Count the attempt now, so a call that fails still uses up the limits.
        self.calls += 1
        self.last_call_at = self.clock()
        self._charged = estimate_tokens(messages)
        self.tokens += self._charged
        self.in_flight, self.inbox = self.inbox, []
        runner.log(
            "strategist",
            f"ask (call {self.calls}, {len(self.in_flight)} trigger(s))",
            {"strategist": {"event": "ask", "call": self.calls, "triggers": self.in_flight, "messages": messages}},
        )
        self._requests.put(messages)

    def _settle(self, runner: Any, answer: Answer) -> None:
        """Charge the real token count, then apply the answer or put its triggers back.

        ``params`` merge onto the current ones at once, bounded by the directives floor as it is now.
        ``goals`` replace the stack only when there is at least one valid goal and
        directives ``goals`` do not own the stack.
        """
        triggers, self.in_flight = self.in_flight or [], None
        reported = tokens_used(answer.usage)
        if reported is not None:
            self.tokens += reported - self._charged
        self._charged = 0
        record: dict[str, Any] = {
            "call": self.calls,
            "raw": answer.raw,
            "usage": answer.usage,
            "tokens_total": self.tokens,
        }
        reply = None
        if not answer.error:
            try:
                reply = json.loads(answer.raw)
            except ValueError as e:
                answer.error = f"reply is not JSON: {e}"
        if answer.error:
            # Retry these triggers on the next allowed call.
            self.inbox = (triggers + self.inbox)[-INBOX_KEPT:]
            log.warning("strategist: call failed: %s", answer.error)
            runner.log("strategist", f"failed: {answer.error}", {"strategist": {"event": "error", "error": answer.error, **record}})
            return
        d = runner.directives.directives
        goals, params, notes = parse_plan_payload(
            reply, floor_params=dict(d.params), current_params=runner.plan.params
        )
        runner.plan.params = params
        record.update(goals=goals, params=runner.plan.params, notes=notes)
        if not goals:
            runner.log("strategist", "answer had no valid goals; stack kept", {"strategist": {"event": "kept", **record}})
            return
        if directive_stack_ops(d.goals):
            runner.log("strategist", "directives goals own the stack; stack kept", {"strategist": {"event": "kept", **record}})
            return
        runner.plan = Plan(
            list(goals),
            dict(runner.plan.params),
            notes=notes,
            floor_params=dict(d.params),
            tick_hz=runner.tick_hz,
        )
        runner.mem.path, runner.mem.goal, runner.mem.goal_op = [], "", None
        runner.log("strategist", f"plan replaced ({len(goals)} goals)", {"strategist": {"event": "applied", **record}})
