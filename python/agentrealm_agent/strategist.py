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
   dropped, idle (from ``Memory``), and map change, hurt and timer (raised here).
2. If an answer came back, apply it (see :meth:`Strategist._settle`).
3. If nothing is in flight, the inbox has triggers and the budget allows it,
   build the prompt, log it to the trace, and hand it to the background thread.

The op table in ``plan.py`` (``OP_FIELDS``) is the contract with the model.
When the planner needs something the states cannot do, add an op there and
the state that runs it; never a state that starts itself.

Settings (environment variables, defaults in brackets):

- ``AGENTREALM_PLANNER_PROVIDER`` [anthropic, or openai when only an
  OpenAI key is set]: ``anthropic`` or ``openai``.
- ``AGENTREALM_PLANNER_MODEL`` [claude-sonnet-5-5 for anthropic; required for openai].
- The provider's key: ``AGENTREALM_PLANNER_ANTHROPIC_KEY``, else
  ``ANTHROPIC_API_KEY``; ``AGENTREALM_PLANNER_OPENAI_KEY``, else
  ``OPENAI_API_KEY`` (:data:`KEY_ENV`). The planner's own names come first
  because some hosts strip the standard ones from the environment.
- ``AGENTREALM_PLANNER_EFFORT`` [low]: Anthropic effort level.
- ``AGENTREALM_PLANNER_REPLAN_S`` [15]: with no event, replan this often.
- ``AGENTREALM_PLANNER_CALLS_PER_MIN`` [6] and
  ``AGENTREALM_PLANNER_TOKENS_PER_MIN`` [the larger of 40000 and 1.5 times
  the cached prefix]: the budget, over the last 60 seconds of play, every
  attempt counted. Uncached input (cache writes included) plus answer tokens
  as the API reports them, cache reads left out (see below); a call that
  reports none is charged its uncached prompt at 4 characters a token. A
  rolling minute, so a long session never runs dry.
- ``AGENTREALM_PLANNER_HURT_FRACTION`` [0.5]: dropping below this share of
  max health raises ``hurt``.
- ``AGENTREALM_PLANNER_IDLE_MINUTES`` [10]: no applied Step for this long
  raises ``idle``.

- ``AGENTREALM_PLANNER_REFERENCE_SECTIONS`` [core, then the rest under
  about 60k tokens]: which parts of the game reference go in the prompt;
  ``all``, ``core``, or a comma list (see :mod:`.planner_reference`).

The system prompt is the game reference, then the measured facts in
``docs/GAME_NOTES.md``, then the progression stages (:data:`PROGRESSION`),
then the op contract below (:func:`system_prompt`).
It is the same on every call, so it is cached: ``cache_control`` on
Anthropic, a fixed ``prompt_cache_key`` on OpenAI (which caches long
prefixes on its own). Only the user message (triggers, state, stack) changes.
``tokens_per_min`` counts uncached input (cache writes included: Anthropic
``cache_creation_input_tokens``, OpenAI prompt tokens not reported cached)
plus output. Cache reads are not counted: they cost a tenth of input and
``calls_per_min`` bounds them. Unset, the budget is the larger of 40000 and
1.5 times the prefix, so the first call and a cache miss never silence the
planner (:func:`default_tokens_per_min`).

Failures: :meth:`Strategist.check` makes one call before play, and a key
the provider refuses (401 or 403) stops the run with one line
(:class:`PlannerAuthError`). A call that fails mid-run backs off: the next
waits :data:`BACKOFF_BASE_S`, doubling with each failure in a row up to
:data:`BACKOFF_CAP_S`. Acceptance runs count every failure and every
accepted plan (``acceptance.PlannerHealth``).

Another provider is a class with the same ``complete(messages)`` and
``check()`` methods (``LLMClient``), picked in :func:`make_client`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Protocol

from .directives import Directives
from .travel.resolve import travel_dest
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .gem_yield import summary as gem_yield_summary
from .planner_reference import game_notes_text, reference_text
from .plan import OP_FIELDS, MAX_WAIT_SECONDS, Plan, parse_plan_payload
from .world import WorldModel

log = logging.getLogger(__name__)

INBOX_KEPT = 48  # newest triggers kept while waiting for a call
CHARS_PER_TOKEN = 4  # estimate for a call that reports no usage
BUDGET_WINDOW_S = 60.0  # the budget counts calls and tokens over this much play
DEFAULT_REPLAN_S = 15.0
DEFAULT_CALLS_PER_MIN = 6
DEFAULT_TOKENS_PER_MIN = 40_000  # floor; the default also fits one prefix write (default_tokens_per_min)
PREFIX_BUDGET_FACTOR = 1.5
DEFAULT_HURT_FRACTION = 0.5
DEFAULT_IDLE_MINUTES = 10
# After a failed call the next waits BACKOFF_BASE_S, doubling per failure in a row, at most BACKOFF_CAP_S.
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 120.0
AUTH_STATUSES = (401, 403)  # the provider refused the key: fatal at startup
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "low"
PROVIDERS = ("anthropic", "openai")
# Where each provider's key is read from, first set wins.
KEY_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("AGENTREALM_PLANNER_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"),
    "openai": ("AGENTREALM_PLANNER_OPENAI_KEY", "OPENAI_API_KEY"),
}
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_PROMPT_CACHE_KEY = "agentrealm-planner"
ANTHROPIC_MAX_TOKENS = 16_000
# Models that take the server-side refusal fallback (beta header plus `fallbacks`).
ANTHROPIC_FALLBACK_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"})
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _op_table() -> str:
    return "\n".join(f"- {name}: {fields}" for name, fields in OP_FIELDS.items())


SYSTEM_PROMPT = f"""You are the planner for an Agent Realm character. Plan from the game reference above: it is the game's own documentation of its rules, intents, combat, survival, items and maps. Where it and the measured facts disagree, trust the measured facts. You own its goal stack: the states work on the op on top. With an empty stack you are not steering: the dispatcher's safe default runs (exploring in safe ground).

Reply with one JSON object only, no markdown, with these keys:
- "goals": the whole new goal stack, top first. Omit the key to keep the current stack. An empty or invalid list clears it.
- "params": survival params to change, starting from the current values under State; omit it to keep them
- "notes": optional string for the trace

Each goal is an object with "op" and that op's fields; every op may also carry "why". These are the only ops (anything else is dropped):
{_op_table()}

State lists the current stack, each op marked "pinned" or "planner". Pinned ops come from the directives file (the user's manual steering, or the run's own target). You cannot remove, reorder or replace them: whatever you send, they stay on top, until they are done or stuck detection gives up on their target. Plan around them. Your "goals" are only your own part of the stack, the ops below the pinned ones; leave pinned ops out of it. Never send a travel, of any kind, whose destination is a cell listed under given_up_travel: stuck detection gave up on it this run.

"wait" needs a "why" and at most {MAX_WAIT_SECONDS} seconds. You are asked again on every event and every few seconds, so plan the next few steps, not the whole game.

Examples:
{{"goals":[{{"op":"buy","code":"torch","why":"clue mentions darkness"}}],"params":{{"curiosity":0.3}},"notes":"try the cave entrance"}}
{{"goals":[{{"op":"buy","code":"small_potion","why":"potions before the boss"}},{{"op":"equip","why":"wear the new armor"}}]}}
"""


PROGRESSION = """# Progression

The character's arc, in order. Judge the stage from State (health, gems, armed, worn, held, lives, map_level, levels_cleared, level_count), the plan and the clues, then pick ops that advance that stage. Move on only when its readiness is met; drop back a stage when it no longer is (after a death, say). The thresholds are guidance for you to apply, not rules the code checks.

1. Survive and learn. Explore safe ground, read signs, talk to NPCs, map the town (explore_area, travel, read, say). Ready when the town's shop and at least one level entrance are known.
2. Build up loot, gear and supplies. Gem hunting is the main work here: cut grass and bushes (a gem drops 10% of the time in ring 1, 15% farther out; field work makes about 3 gems a minute), fell trees, take gem piles and break gem caches. Pick up food along the way. Then buy potions and gear and equip the best (gather_gems, break_block, fetch_item, buy, equip). Ready when health is at least 80% of max, at least 3 potions are held, a weapon better than the starting weapon is armed, and armor is worn (State: health, held, armed, worn).
3. Beat levels. When geared, enter a level door, solve it, fight its boss (travel, enter_level, break_block, use_block, compose, fight_boss). Restock (stage 2) between levels and whenever health or potions fall below the stage 2 bar.
4. Beat the world. Clear every level to transcend: done when levels_cleared holds level_count levels.

Gems by area (stage 2). Gem drops from grass and bushes vary by area, and some areas drop none. State gem_yield is measured from the character's own cuts: the best regions nearby with their yield (gems per cut) and the barren ones. Hunt gems where the yield is good, leave a region that shows no gems after a fair sample (Gather skips barren regions unless gather_gems names one with x, y), and explore regions not yet sampled to sample them."""


def system_prompt(reference_sections: str = "") -> str:
    """The stable prefix: the game reference, the measured facts, then the op contract."""
    parts = []
    reference = reference_text(reference_sections)
    if reference:
        parts.append(reference.strip())
    notes = game_notes_text()
    if notes:
        parts.append(
            "# Measured facts (docs/GAME_NOTES.md)\n\n"
            "What we measured in play. Prefer these over the reference when they disagree.\n\n" + notes.strip()
        )
    parts.append(PROGRESSION)
    parts.append("# Planner contract\n\n" + SYSTEM_PROMPT.strip())
    return "\n\n".join(parts) + "\n"


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]: ...

    def check(self) -> None:
        """One cheap authenticated call; raises what the provider raised."""


class PlannerConfigError(ValueError):
    """The planner is on but cannot run (no key, no SDK). One line, for the CLI to print."""


class PlannerAuthError(PlannerConfigError):
    """The provider refused the planner's key (401 or 403) on the startup check."""


class ProviderHTTPError(RuntimeError):
    """A provider answered with an HTTP error status."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status_code = status


def error_status(e: BaseException) -> int | None:
    """The HTTP status of a provider error (``anthropic`` SDK or ours), else None."""
    status = getattr(e, "status_code", None)
    return status if isinstance(status, int) else None


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
    tokens_per_min: int = 0  # 0: default_tokens_per_min(reference_sections)
    hurt_fraction: float = DEFAULT_HURT_FRACTION
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    reference_sections: str = ""  # "" core then the rest under budget, "all", "core", or a comma list

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
            tokens_per_min=int(number("AGENTREALM_PLANNER_TOKENS_PER_MIN", 0)),
            hurt_fraction=number("AGENTREALM_PLANNER_HURT_FRACTION", DEFAULT_HURT_FRACTION),
            idle_minutes=number("AGENTREALM_PLANNER_IDLE_MINUTES", DEFAULT_IDLE_MINUTES),
            reference_sections=os.environ.get("AGENTREALM_PLANNER_REFERENCE_SECTIONS", "").strip(),
        )


def default_tokens_per_min(reference_sections: str = "") -> int:
    """The per-minute token budget when none is set: room for one full prefix write."""
    prefix = estimate_tokens([{"role": "system", "content": system_prompt(reference_sections)}])
    return max(DEFAULT_TOKENS_PER_MIN, math.ceil(PREFIX_BUDGET_FACTOR * prefix))


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

    def check(self) -> None:
        """Read the model: 401 or 403 means the key is refused."""
        req = urllib.request.Request(
            f"{OPENAI_MODELS_URL}/{self.model}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        self._send(req, timeout=30)

    def _send(self, req: urllib.request.Request, *, timeout: float) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise ProviderHTTPError(f"openai http {e.code}: {detail}", e.code) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"openai network: {e}") from e

    def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                "response_format": {"type": "json_object"},
                # OpenAI caches a long shared prefix itself; the key keeps our calls on one cache.
                "prompt_cache_key": OPENAI_PROMPT_CACHE_KEY,
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
        payload = self._send(req, timeout=120)
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("openai: empty choices")
        content = choices[0].get("message", {}).get("content", "")
        if not isinstance(content, str):
            raise RuntimeError("openai: missing message content")
        usage = dict(payload["usage"]) if isinstance(payload.get("usage"), dict) else {}
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens"):
            # Report the cached prefix apart, as Anthropic does, so the budget skips it.
            usage["cache_read_input_tokens"] = details["cached_tokens"]
            usage["prompt_tokens"] = max(0, int(usage.get("prompt_tokens", 0)) - int(details["cached_tokens"]))
        return content.strip(), usage


class AnthropicClient:
    """Claude through the official ``anthropic`` SDK (``pip install anthropic``)."""

    def __init__(self, api_key: str, model: str, effort: str = DEFAULT_EFFORT) -> None:
        import anthropic  # the planner's one optional dependency (AGENTS.md)

        self.sdk = anthropic.Anthropic(api_key=api_key, max_retries=1)
        self.model = model
        self.effort = effort

    def check(self) -> None:
        """Read the model: the SDK raises ``AuthenticationError`` (401) or
        ``PermissionDeniedError`` (403) for a refused key."""
        self.sdk.models.retrieve(self.model)

    def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        system = [anthropic_system_block(m) for m in messages if m["role"] == "system"]
        turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
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
        usage = {
            "input_tokens": resp.usage.input_tokens,  # uncached input only; the cache counts are apart
            "output_tokens": resp.usage.output_tokens,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        }
        return text.strip(), usage


def anthropic_system_block(message: dict[str, Any]) -> dict[str, Any]:
    """A system message as an Anthropic text block, with ``cache_control`` when marked ``cache``."""
    block: dict[str, Any] = {"type": "text", "text": message["content"]}
    if message.get("cache"):
        block["cache_control"] = {"type": "ephemeral"}
    return block


def make_client(cfg: StrategistConfig) -> LLMClient:
    """The provider client for ``cfg``. Add another provider here."""
    if cfg.provider == "anthropic":
        return AnthropicClient(cfg.api_key, cfg.model, cfg.effort)
    return OpenAIChatClient(cfg.api_key, cfg.model)


def tokens_used(usage: dict[str, Any]) -> int | None:
    """Uncached prompt (cache writes included) plus answer tokens the API reported,
    or None when it reported none. Cache reads are not counted.

    OpenAI names them ``prompt_tokens``/``completion_tokens`` (the client takes
    cached reads out of ``prompt_tokens``), Anthropic ``input_tokens``/
    ``output_tokens`` with writes apart in ``cache_creation_input_tokens``.
    """
    keys = ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "cache_creation_input_tokens")
    try:
        total = sum(int(usage.get(k, 0) or 0) for k in keys)
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


def same_ops(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """Whether two stacks are the same plan: equal ops, ignoring each op's
    free-text ``why``, so a reworded reason does not restart a ``wait``."""

    def key(op: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in op.items() if k != "why"}

    return len(a) == len(b) and all(key(x) == key(y) for x, y in zip(a, b))


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Characters over 4 for the part of the prompt that is not cached."""
    return sum(len(msg["content"]) for msg in messages if not msg.get("cache")) // CHARS_PER_TOKEN + 1


def trace_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The prompt for the trace, with the cached prefix as its size and digest, not its text."""
    out = []
    for msg in messages:
        if msg.get("cache"):
            digest = hashlib.sha256(msg["content"].encode("utf-8")).hexdigest()[:12]
            msg = {**msg, "content": f"<cached prefix: {len(msg['content'])} chars, sha256 {digest}>"}
        out.append(msg)
    return out


def build_prompt(
    *,
    triggers: list[dict[str, Any]],
    w: WorldModel,
    plan: Plan,
    directives: Directives,
    knowledge: KnowledgeBase | None,
    reference_sections: str = "",
    given_up_travel: Collection[tuple[int, tuple[int, int]]] = (),
) -> list[dict[str, Any]]:
    """The model's input: the cached system prefix (:func:`system_prompt`), then
    one user message with triggers, state, the remaining plan, every clue, and instructions."""
    pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos and w.map_id is not None else "unknown"
    state_lines = [
        f"tick={w.tick} pos={pos} alive={w.alive} health={w.health}/{w.max_health} gems={w.gems}",
        f"map_level={w.map_level} armed={w.armed_code} lives={w.lives}",
        f"worn={json.dumps(w.worn_codes, sort_keys=True)} held={json.dumps(dict(sorted(Counter(s.code for s in w.held_supplies).items())))}",
        f"levels_cleared={w.levels_cleared} level_count={w.level_count}",
        f"gem_yield={json.dumps(gem_yield_summary(w, knowledge), sort_keys=True)}",
        f"params={json.dumps(plan.params, sort_keys=True)}",
        f"params_floor={json.dumps(directives.params, sort_keys=True)} (survival params may only tighten past these)",
    ]
    if plan.notes:
        state_lines.append(f"plan_notes={plan.notes!r}")
    lines = stack_lines(plan)
    state_lines.append("stack (top first):" + "".join(f"\n  {line}" for line in lines) if lines else "stack: (empty)")
    if given_up_travel:
        cells = [f"{mid}:{x},{y}" for mid, (x, y) in sorted(given_up_travel, key=str)]
        state_lines.append(f"given_up_travel={json.dumps(cells)} (cells stuck detection gave up on: never travel to them again this run)")
    clues: list[dict[str, Any]] = []
    if knowledge is not None:
        with knowledge.lock:
            clues = list(knowledge.clues)
    user_parts = [
        "Triggers:\n" + json.dumps(triggers, sort_keys=True),
        "State:\n" + "\n".join(state_lines),
        "Clues (oldest first):\n" + json.dumps(clues, sort_keys=True),
        "Directives instructions:\n" + (directives.instructions or "(none)"),
    ]
    return [
        {"role": "system", "content": system_prompt(reference_sections), "cache": True},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def stack_lines(plan: Plan) -> list[str]:
    """The ops left on the stack, top first, each marked ``pinned`` (a
    directives op, which the planner cannot remove) or ``planner`` (A35)."""
    return [
        f"{'pinned' if i < plan.directive_end else 'planner'} {json.dumps(op, sort_keys=True)}"
        for i, op in enumerate(plan.goals[plan.index :], plan.index)
    ]


@dataclass
class Answer:
    """What the background thread hands back for one call."""

    raw: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""  # set when the call failed
    status: int | None = None  # the provider's HTTP status on a failed call, when it gave one


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
    failures_in_a_row: int = 0  # failed calls since the last good one; sets the backoff
    retry_at: float = 0.0  # after a failure, no call before this clock time
    spent: deque = field(default_factory=deque)  # [sent_at, tokens] per call in the budget window
    _last_map: tuple[int, int | None] | None = None  # (map_id, level) at the last window
    _hurt: bool = False
    _token_budget: int = 0  # resolved tokens_per_min (token_budget)
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
                f"planner: provider {cfg.provider} needs its SDK: run `make setup` (pip install -r python/requirements.txt), or pass --no-planner"
            ) from None
        return cls(config=cfg, client=client)

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.client is not None

    def check(self) -> None:
        """One call before play. Raises :class:`PlannerAuthError` when the
        provider refuses the key (401 or 403), and :class:`PlannerConfigError`
        on any other 4xx but 429 (a typo'd model is a 404). Rate limits,
        server errors and the network are left to the run, which counts them
        and backs off."""
        if not self.enabled:
            return
        assert self.client is not None
        try:
            self.client.check()
        except Exception as e:  # network, HTTP status
            status = error_status(e)
            if status in AUTH_STATUSES:
                names = " or ".join(KEY_ENV[self.config.provider])
                raise PlannerAuthError(
                    f"planner: {self.config.provider} refused the key (HTTP {status}); fix {names}, or pass --no-planner"
                ) from None
            if status is not None and 400 <= status < 500 and status != 429:
                raise PlannerConfigError(
                    f"planner: {self.config.provider} rejected model {self.config.model!r} (HTTP {status}); "
                    "fix AGENTREALM_PLANNER_MODEL, or pass --no-planner"
                ) from None
            log.warning("strategist: startup check failed: %s", e)

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

    def _ask(self, messages: list[dict[str, Any]]) -> Answer:
        assert self.client is not None
        answer = Answer()
        try:
            answer.raw, answer.usage = self.client.complete(messages)
        except Exception as e:  # network, HTTP status, refusal
            answer.error = str(e) or type(e).__name__
            answer.status = error_status(e)
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
        if self.clock() < self.retry_at:
            return  # backing off after a failed call
        if self.inbox and not self.limit_reached():
            self._send(runner)

    def token_budget(self) -> int:
        """``tokens_per_min``, or :func:`default_tokens_per_min` when it is unset."""
        if not self._token_budget:
            self._token_budget = self.config.tokens_per_min or default_tokens_per_min(self.config.reference_sections)
        return self._token_budget

    def limit_reached(self) -> str:
        """Which per-minute budget stops a call now, or "" when one may start."""
        now = self.clock()
        while self.spent and now - self.spent[0][0] >= BUDGET_WINDOW_S:
            self.spent.popleft()
        if len(self.spent) >= self.config.calls_per_min:
            return "calls_per_min"
        if sum(tokens for _, tokens in self.spent) >= self.token_budget():
            return "tokens_per_min"
        return ""

    def _collect(self, runner: Any) -> None:
        """Move queued signals into the inbox and raise the map, hurt and idle triggers."""
        w, m = runner.world, runner.mem
        where = (w.map_id, w.map_level) if w.map_id is not None else None
        if where is not None and where != self._last_map:
            # Every change of map or level, re-entering one included (after a death, a retry).
            self._last_map = where
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

    @staticmethod
    def _report(acceptance: Any, reply: Any, goals: list[dict[str, Any]], record: dict[str, Any]) -> None:
        """Tell the acceptance hooks what the reply was: a plan accepted (at
        least one valid op, or an explicit empty stack), an error (not a JSON
        object, or ops all invalid), or neither (no ``goals`` key: notes or
        params only, the stack kept)."""
        if not isinstance(reply, dict):
            acceptance.on_strategist_error(record.get("invalid") or "reply is not a JSON object")
            return
        if "goals" not in reply:
            return
        sent = reply["goals"]
        if goals or sent == []:
            acceptance.on_strategist_reply()
        else:
            acceptance.on_strategist_error("reply has no valid goal op")

    def _send(self, runner: Any) -> None:
        messages = build_prompt(
            triggers=self.inbox,
            w=runner.world,
            plan=runner.plan,
            directives=runner.directives.directives,
            knowledge=runner.knowledge,
            reference_sections=self.config.reference_sections,
            given_up_travel=runner.mem.nav_stuck.given_up_travel,
        )
        # Charge the attempt now, so a call that fails still uses up the budget.
        self.calls += 1
        self.last_call_at = self.clock()
        self.spent.append([self.last_call_at, estimate_tokens(messages)])
        self.in_flight, self.inbox = self.inbox, []
        runner.log(
            "strategist",
            f"ask (call {self.calls}, {len(self.in_flight)} trigger(s))",
            {"strategist": {"event": "ask", "call": self.calls, "triggers": self.in_flight, "messages": trace_messages(messages)}},
        )
        self._requests.put(messages)

    def _settle(self, runner: Any, answer: Answer) -> None:
        """Charge the real token count, then apply the answer.

        A failed call (network, HTTP, refusal) keeps the stack and puts its
        triggers back for the next call. Otherwise ``params`` merge onto the
        current ones at once, bounded by the directives floor as it is now,
        and ``goals``, when the reply has the key, become the stack. A stack
        equal to the one left keeps its progress, and the same op on top
        keeps its own (stall clock, wait start, block snapshot, path). No
        valid goal (or a reply that is not a JSON object) clears it: the
        planner layer emits nothing and the dispatcher's safe default runs.
        Directives ``goals`` override the planner: the directives ops still
        left stay on top, and the planner's goals go below them (A35).
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
            self.failures_in_a_row += 1
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (self.failures_in_a_row - 1))
            self.retry_at = self.clock() + delay
            log.warning("strategist: call failed: %s", answer.error)
            runner.log(
                "strategist",
                f"failed: {answer.error}; next call in {delay:.0f}s",
                {"strategist": {"event": "error", "error": answer.error, "backoff_s": delay, **record}},
            )
            if runner.acceptance is not None:
                runner.acceptance.on_strategist_error(answer.error, auth=answer.status in AUTH_STATUSES)
            return
        self.failures_in_a_row, self.retry_at = 0, 0.0
        try:
            reply = parse_reply(answer.raw)
        except ValueError as e:
            reply, record["invalid"] = None, f"reply is not JSON: {e}"
        d = runner.directives.directives
        goals, params, notes = parse_plan_payload(
            reply, floor_params=dict(d.params), current_params=runner.plan.params
        )
        if runner.acceptance is not None:
            self._report(runner.acceptance, reply, goals, record)
        runner.plan.params = params
        record.update(goals=goals, params=runner.plan.params, notes=notes)
        if isinstance(reply, dict) and "goals" not in reply:
            runner.log("strategist", "no goals in reply; stack kept", {"strategist": {"event": "kept", **record}})
            return
        given_up = runner.mem.nav_stuck.given_up_travel
        if given_up:
            # A travel to a cell stuck detection gave up on never reaches the
            # stack (A16): filtered here, so a re-send raises no goal_failed
            # and cannot set off another call.
            def dest(g):
                return travel_dest(g, runner.world, runner.knowledge, runner.mem.strength)

            kept = [g for g in goals if dest(g) not in given_up]
            if len(kept) < len(goals):
                record["given_up_filtered"] = [g for g in goals if g not in kept]
                goals = kept
        old = runner.plan
        pinned = old.directive_ops()  # directives ops left: they stay on top
        # A reply that repeats a directives op does not stack it twice.
        goals = pinned + [g for g in goals if not any(same_ops([g], [p]) for p in pinned)]
        if same_ops(goals, old.goals[old.index :]):
            # A timer reply that re-sends the stack (or leaves an empty one
            # empty): keep its progress (stall clock, wait start, block
            # snapshot) and the path being walked.
            old.notes = notes or old.notes
            runner.log("strategist", "same stack; progress kept", {"strategist": {"event": "unchanged", **record}})
            return
        runner.plan = Plan(
            list(goals),
            dict(old.params),
            notes=notes,
            floor_params=dict(d.params),
            tick_hz=runner.tick_hz,
            directive_end=len(pinned),
        )
        head = old.current()
        if goals and head is not None and same_ops(goals[:1], [head]):
            # Same op on top: it carries on where it was; only the ops below it changed.
            # Keep the old op itself (its `why` too), so the path set for it stays owned.
            runner.plan.goals[0] = head
            runner.plan.wait_started_tick = old.wait_started_tick
            runner.plan.stalled_since_tick = old.stalled_since_tick
            runner.plan.block_before = old.block_before
        else:
            runner.mem.path, runner.mem.goal, runner.mem.goal_op = [], "", None
            runner.mem.walks.clear()  # the new head walks a path of its own (A15)
        if not goals:
            runner.log("strategist", "no valid goals; stack cleared (dispatcher safe default)", {"strategist": {"event": "cleared", **record}})
            return
        below = f", under {len(pinned)} directives op(s)" if pinned else ""
        runner.log("strategist", f"plan replaced ({len(goals) - len(pinned)} goals{below})", {"strategist": {"event": "applied", **record}})
        if runner.acceptance is not None:
            runner.acceptance.on_strategist_applied(goals)
