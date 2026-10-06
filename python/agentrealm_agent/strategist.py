"""Strategist: the AI planner that owns the goal stack (A35).

On in every live run. ``run --no-planner`` (or ``AGENTREALM_NO_PLANNER=1``)
is a test mode: triggers are drained and logged, and the built-in plan from
``policy.goals`` plus the no-LLM clue rules (A32) stay in charge. With the
planner on and no key, :meth:`Strategist.from_env` raises
:class:`PlannerConfigError` with one line, and the CLI stops before it plays.

Two threads, one rule: everything that reads or writes the world, the plan,
``Memory`` or the trace runs on the tick thread, in :meth:`Strategist.on_window`.
The background thread only sends a prompt and waits for the reply. The two
threads talk through two queues, a request and an answer, so a slow model
never costs a tick and nothing needs a lock.

One window, on the tick thread:

1. Move new triggers into the inbox: clue, stuck, death, goal done or
   dropped, idle (from ``Memory``), and new map, hurt and timer (raised here).
2. If an answer came back, apply it (see :meth:`Strategist._settle`).
3. If nothing is in flight, the inbox has triggers and the budget allows it,
   build the prompt, log it to the trace, and hand it to the background thread.

The op table in ``plan.py`` (``OP_FIELDS``) is the contract with the model.
When the planner needs something the states cannot do, add an op there and
the state that runs it; never a state that starts itself.

Settings (environment variables, defaults in brackets):

- ``AGENTREALM_PLANNER_PROVIDER`` [anthropic, or openai when only
  ``OPENAI_API_KEY`` is set]: ``anthropic`` or ``openai``.
- ``AGENTREALM_PLANNER_MODEL`` [claude-sonnet-5-5 for anthropic; required for openai].
- The provider's key: ``AGENTREALM_PLANNER_ANTHROPIC_KEY``, else
  ``ANTHROPIC_API_KEY``; ``AGENTREALM_PLANNER_OPENAI_KEY``, else
  ``OPENAI_API_KEY`` (:data:`KEY_ENV`). The planner's own names come first
  because some hosts strip the standard ones from the environment.
- ``AGENTREALM_PLANNER_EFFORT`` [low]: Anthropic effort level.
- ``AGENTREALM_PLANNER_REPLAN_S`` [15]: with no event, replan this often.
- ``AGENTREALM_PLANNER_CALLS_PER_MIN`` [6] and
  ``AGENTREALM_PLANNER_TOKENS_PER_MIN`` [40000]: the budget, over the last
  60 seconds of play, every attempt counted. Prompt plus answer tokens as the
  API reports them; a call that reports none is charged its prompt at 4
  characters a token. A rolling minute, so a long session never runs dry.
- ``AGENTREALM_PLANNER_HURT_FRACTION`` [0.5]: dropping below this share of
  max health raises ``hurt``.
- ``AGENTREALM_PLANNER_IDLE_MINUTES`` [10]: no applied Step for this long
  raises ``idle``.

Another provider is a class with the same ``complete(messages)`` method
(``LLMClient``), picked in :func:`make_client`.
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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .directives import Directives
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .plan import OP_FIELDS, WAIT_MAX_SECONDS, Plan, directive_stack_ops, parse_plan_payload
from .world import WorldModel

log = logging.getLogger(__name__)

INBOX_KEPT = 48  # newest triggers kept while waiting for a call
CHARS_PER_TOKEN = 4  # estimate for a call that reports no usage
BUDGET_WINDOW_S = 60.0  # the budget counts calls and tokens over this much play
DEFAULT_REPLAN_S = 15.0
DEFAULT_CALLS_PER_MIN = 6
DEFAULT_TOKENS_PER_MIN = 40_000
DEFAULT_HURT_FRACTION = 0.5
DEFAULT_IDLE_MINUTES = 10
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "low"
PROVIDERS = ("anthropic", "openai")
# Where each provider's key is read from, first set wins.
KEY_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("AGENTREALM_PLANNER_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"),
    "openai": ("AGENTREALM_PLANNER_OPENAI_KEY", "OPENAI_API_KEY"),
}
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_MAX_TOKENS = 16_000
# Models that take the server-side refusal fallback (beta header plus `fallbacks`).
ANTHROPIC_FALLBACK_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"})
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _op_table() -> str:
    return "\n".join(f"- {name}: {fields}" for name, fields in OP_FIELDS.items())


SYSTEM_PROMPT = f"""You are the planner for an Agent Realm character. You own its goal stack: the states run only the op on top, and with no plan the character explores safe ground and does nothing else.

Reply with one JSON object only, no markdown, with these keys:
- "goals": the whole new goal stack, top first. Omit the key to keep the current stack. An empty or invalid list clears it.
- "params": survival params to change, starting from the current values under State; omit it to keep them
- "notes": optional string for the trace

Each goal is an object with "op" and that op's fields; every op may also carry "why". These are the only ops (anything else is dropped):
{_op_table()}

"wait" needs a "why" and at most {WAIT_MAX_SECONDS} seconds. You are asked again on every event and every few seconds, so plan the next few steps, not the whole game.

Example:
{{"goals":[{{"op":"buy","code":"torch","why":"clue mentions darkness"}}],"params":{{"curiosity":0.3}},"notes":"try the cave entrance"}}
"""


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]: ...


class PlannerConfigError(ValueError):
    """The planner is on but cannot run (no key, no SDK). One line, for the CLI to print."""


def provider_key(provider: str) -> str:
    """The provider's key from the first variable in :data:`KEY_ENV` that is set."""
    for name in KEY_ENV[provider]:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def planner_disabled_by_env() -> bool:
    return os.environ.get("AGENTREALM_NO_PLANNER", "").strip().lower() in ("1", "true", "yes")


@dataclass
class StrategistConfig:
    provider: str = ""  # "" means off (the --no-planner test mode)
    model: str = ""
    api_key: str = ""
    effort: str = DEFAULT_EFFORT
    replan_s: float = DEFAULT_REPLAN_S
    calls_per_min: int = DEFAULT_CALLS_PER_MIN
    tokens_per_min: int = DEFAULT_TOKENS_PER_MIN
    hurt_fraction: float = DEFAULT_HURT_FRACTION
    idle_minutes: float = DEFAULT_IDLE_MINUTES

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.model and self.api_key)

    @classmethod
    def from_env(cls) -> StrategistConfig:
        """Read the planner settings. Raises :class:`PlannerConfigError` when one is missing."""
        provider = os.environ.get("AGENTREALM_PLANNER_PROVIDER", "").strip().lower()
        if not provider:
            provider = "openai" if provider_key("openai") and not provider_key("anthropic") else "anthropic"
        if provider not in PROVIDERS:
            raise PlannerConfigError(
                f"planner: AGENTREALM_PLANNER_PROVIDER={provider!r}; use one of {', '.join(PROVIDERS)}"
            )
        api_key = provider_key(provider)
        if not api_key:
            names = " or ".join(KEY_ENV[provider])
            raise PlannerConfigError(f"planner: set {names} (provider {provider}), or pass --no-planner")
        model = os.environ.get("AGENTREALM_PLANNER_MODEL", "").strip()
        if not model and provider == "anthropic":
            model = DEFAULT_ANTHROPIC_MODEL
        if not model:
            raise PlannerConfigError(f"planner: set AGENTREALM_PLANNER_MODEL for provider {provider}")

        def number(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise PlannerConfigError(f"planner: {name}={raw!r} is not a number") from None

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            effort=os.environ.get("AGENTREALM_PLANNER_EFFORT", "").strip() or DEFAULT_EFFORT,
            replan_s=number("AGENTREALM_PLANNER_REPLAN_S", DEFAULT_REPLAN_S),
            calls_per_min=int(number("AGENTREALM_PLANNER_CALLS_PER_MIN", DEFAULT_CALLS_PER_MIN)),
            tokens_per_min=int(number("AGENTREALM_PLANNER_TOKENS_PER_MIN", DEFAULT_TOKENS_PER_MIN)),
            hurt_fraction=number("AGENTREALM_PLANNER_HURT_FRACTION", DEFAULT_HURT_FRACTION),
            idle_minutes=number("AGENTREALM_PLANNER_IDLE_MINUTES", DEFAULT_IDLE_MINUTES),
        )


def drain_triggers(m: Memory) -> list[dict[str, Any]]:
    """Take every queued trigger (clue, stuck, and strategist signals). Tick thread only."""
    out = m.clue_signals + m.nav_stuck.stuck_signals + m.strategist_signals
    m.clue_signals, m.nav_stuck.stuck_signals, m.strategist_signals = [], [], []
    return out


class OpenAIChatClient:
    """OpenAI chat completions (stdlib ``urllib``)."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
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


class AnthropicClient:
    """Claude through the official ``anthropic`` SDK (``pip install anthropic``)."""

    def __init__(self, api_key: str, model: str, effort: str = DEFAULT_EFFORT) -> None:
        import anthropic  # the planner's one optional dependency (AGENTS.md)

        self.sdk = anthropic.Anthropic(api_key=api_key, max_retries=1)
        self.model = model
        self.effort = effort

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": system,
            "messages": turns,
            "extra_body": {"output_config": {"effort": self.effort}},
        }
        if self.model in ANTHROPIC_FALLBACK_MODELS:
            # A request a safety classifier declines is re-run on a fallback model server-side.
            request["betas"] = [ANTHROPIC_FALLBACK_BETA]
            request["extra_body"]["fallbacks"] = "default"
        resp = self.sdk.beta.messages.create(**request)
        if resp.stop_reason == "refusal":
            raise RuntimeError("anthropic: refused")
        text = "".join(block.text for block in resp.content if block.type == "text")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return text.strip(), usage


def make_client(cfg: StrategistConfig) -> LLMClient:
    """The provider client for ``cfg``. Add another provider here."""
    if cfg.provider == "anthropic":
        return AnthropicClient(cfg.api_key, cfg.model, cfg.effort)
    return OpenAIChatClient(cfg.api_key, cfg.model)


def tokens_used(usage: dict[str, Any]) -> int | None:
    """Prompt plus answer tokens the API reported, or None when it reported none.

    OpenAI names them ``prompt_tokens``/``completion_tokens``, Anthropic
    ``input_tokens``/``output_tokens``.
    """
    try:
        total = sum(
            int(usage.get(k, 0)) for k in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
        )
    except (TypeError, ValueError):
        return None
    return total or None


def parse_reply(raw: str) -> Any:
    """The JSON object in a reply; a model without a JSON mode may wrap it in a code fence."""
    try:
        return json.loads(raw)
    except ValueError:
        first, last = raw.find("{"), raw.rfind("}")
        if first < 0 or last < first:
            raise
        return json.loads(raw[first : last + 1])


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
    """Asks the model for a new goal stack on each event and timer tick (see module docstring)."""

    config: StrategistConfig
    client: LLMClient | None = None
    clock: Callable[[], float] = time.monotonic
    inbox: list[dict[str, Any]] = field(default_factory=list)  # triggers not yet sent
    in_flight: list[dict[str, Any]] | None = None  # triggers of the call being answered
    calls: int = 0  # calls this run, for the trace
    last_call_at: float | None = None
    spent: deque = field(default_factory=deque)  # [sent_at, tokens] per call in the budget window
    _maps_seen: set[int] = field(default_factory=set)
    _hurt: bool = False
    _idle_sent_for_tick: int = -1
    _requests: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1), repr=False)
    _answers: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    @classmethod
    def off(cls) -> Strategist:
        """The ``--no-planner`` test mode: triggers are drained and logged, nothing is sent."""
        return cls(config=StrategistConfig())

    @classmethod
    def from_env(cls) -> Strategist:
        """The planner for a live run; off only with ``AGENTREALM_NO_PLANNER``.

        Raises :class:`PlannerConfigError` when it is on and cannot run.
        """
        if planner_disabled_by_env():
            return cls.off()
        cfg = StrategistConfig.from_env()
        try:
            client = make_client(cfg)
        except ImportError:
            raise PlannerConfigError(
                f"planner: provider {cfg.provider} needs `pip install {cfg.provider}`, or pass --no-planner"
            ) from None
        return cls(config=cfg, client=client)

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.client is not None

    # --- background thread: send the prompt, wait for the reply ---

    def start(self) -> None:
        """Start the background thread; in test mode there is nothing to start."""
        if not self.enabled:
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
        except Exception as e:  # network, HTTP status, refusal
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
        if not self.enabled:
            if self.inbox:
                runner.log(
                    "strategist",
                    f"drained {len(self.inbox)} trigger(s), no planner",
                    {"strategist": {"event": "drain", "triggers": self.inbox}},
                )
                self.inbox = []
            return
        if self.in_flight is not None or runner.world.pos is None:
            return  # one call at a time, and nothing to plan from before the first position read
        if self.last_call_at is None or self.clock() - self.last_call_at >= self.config.replan_s:
            if not any(t["trigger"] == "timer" for t in self.inbox):
                self.inbox.append({"trigger": "timer", "tick": runner.world.tick})
        if self.inbox and not self.limit_reached():
            self._send(runner)

    def limit_reached(self) -> str:
        """Which per-minute budget stops a call now, or "" when one may start."""
        now = self.clock()
        while self.spent and now - self.spent[0][0] >= BUDGET_WINDOW_S:
            self.spent.popleft()
        if len(self.spent) >= self.config.calls_per_min:
            return "calls_per_min"
        if sum(tokens for _, tokens in self.spent) >= self.config.tokens_per_min:
            return "tokens_per_min"
        return ""

    def _collect(self, runner: Any) -> None:
        """Move queued signals into the inbox and raise the map, hurt and idle triggers."""
        w, m = runner.world, runner.mem
        if w.map_id is not None and w.map_id not in self._maps_seen:
            self._maps_seen.add(w.map_id)
            self.inbox.append({"trigger": "map", "map_id": w.map_id, "level": w.map_level, "tick": w.tick})
        if w.alive and w.health is not None and w.max_health:
            hurt = w.health < self.config.hurt_fraction * w.max_health
            if hurt and not self._hurt:
                self.inbox.append({"trigger": "hurt", "health": w.health, "max_health": w.max_health, "tick": w.tick})
            self._hurt = hurt
        since = m.strategist_progress_tick
        idle_ticks = max(1, int(self.config.idle_minutes * 60 * runner.tick_hz))
        if since >= 0 and w.tick - since >= idle_ticks and self._idle_sent_for_tick != since:
            self._idle_sent_for_tick = since
            self.inbox.append({"trigger": "idle", "since_tick": since, "tick": w.tick, "idle_ticks": idle_ticks})
        drained = drain_triggers(m)
        if runner.acceptance is not None:
            for trigger in drained:
                runner.acceptance.on_strategist_trigger(trigger)
        self.inbox.extend(drained)
        del self.inbox[:-INBOX_KEPT]

    def _send(self, runner: Any) -> None:
        messages = build_prompt(
            triggers=self.inbox,
            w=runner.world,
            plan=runner.plan,
            directives=runner.directives.directives,
            knowledge=runner.knowledge,
        )
        # Charge the attempt now, so a call that fails still uses up the budget.
        self.calls += 1
        self.last_call_at = self.clock()
        self.spent.append([self.last_call_at, estimate_tokens(messages)])
        self.in_flight, self.inbox = self.inbox, []
        runner.log(
            "strategist",
            f"ask (call {self.calls}, {len(self.in_flight)} trigger(s))",
            {"strategist": {"event": "ask", "call": self.calls, "triggers": self.in_flight, "messages": messages}},
        )
        self._requests.put(messages)

    def _settle(self, runner: Any, answer: Answer) -> None:
        """Charge the real token count, then apply the answer.

        A failed call (network, HTTP, refusal) keeps the stack and puts its
        triggers back for the next call. Otherwise ``params`` merge onto the
        current ones at once, bounded by the directives floor as it is now,
        and ``goals``, when the reply has the key, become the stack. No valid
        goal (or a reply that is not a JSON object) clears it, so the
        dispatcher's safe default runs. Directives ``goals`` own the stack
        and override all of this.
        """
        triggers, self.in_flight = self.in_flight or [], None
        reported = tokens_used(answer.usage)
        if reported is not None and self.spent:
            self.spent[-1][1] = reported
        record: dict[str, Any] = {
            "call": self.calls,
            "raw": answer.raw,
            "usage": answer.usage,
            "tokens_last_minute": sum(tokens for _, tokens in self.spent),
        }
        if answer.error:
            self.inbox = (triggers + self.inbox)[-INBOX_KEPT:]
            log.warning("strategist: call failed: %s", answer.error)
            runner.log("strategist", f"failed: {answer.error}", {"strategist": {"event": "error", "error": answer.error, **record}})
            return
        try:
            reply = parse_reply(answer.raw)
        except ValueError as e:
            reply, record["invalid"] = None, f"reply is not JSON: {e}"
        d = runner.directives.directives
        goals, params, notes = parse_plan_payload(
            reply, floor_params=dict(d.params), current_params=runner.plan.params
        )
        runner.plan.params = params
        record.update(goals=goals, params=runner.plan.params, notes=notes)
        if directive_stack_ops(d.goals):
            runner.log("strategist", "directives goals own the stack; stack kept", {"strategist": {"event": "kept", **record}})
            return
        if isinstance(reply, dict) and "goals" not in reply:
            runner.log("strategist", "no goals in reply; stack kept", {"strategist": {"event": "kept", **record}})
            return
        runner.plan = Plan(
            list(goals),
            dict(runner.plan.params),
            notes=notes,
            floor_params=dict(d.params),
            tick_hz=runner.tick_hz,
        )
        runner.mem.path, runner.mem.goal, runner.mem.goal_op = [], "", None
        if not goals:
            runner.log("strategist", "no valid goals; stack cleared (safe default)", {"strategist": {"event": "cleared", **record}})
            return
        runner.log("strategist", f"plan replaced ({len(goals)} goals)", {"strategist": {"event": "applied", **record}})
        if runner.acceptance is not None:
            runner.acceptance.on_strategist_applied(goals)
