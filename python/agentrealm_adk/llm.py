"""The planner call plumbing both agents share (A140): providers, the call budget, backoff and reply parsing.

Gary's strategist (``agentrealm_agent.strategist``) and Lorraine's one
question (``ladder.ask``) each build their own prompt and read their own
reply; everything under that is here, so neither imports the other's prompt.

- **Providers.** ``LLMClient`` is the one interface: ``complete(messages)``
  gives the reply text and the usage the API reported, ``check()`` makes one
  cheap authenticated call. ``ProviderConfig.from_env`` reads which provider,
  model, key and effort; ``connect`` makes the client and ``check_client``
  stops a run whose key is refused. Another provider is a class with the
  same two methods, picked in :func:`make_client`.
- **The call budget.** ``CallBudget`` counts calls and tokens over the last
  ``BUDGET_WINDOW_S`` of play, every attempt counted, charged an estimate
  when sent (:func:`estimate_tokens`) and settled to what the API reported
  (:func:`tokens_used`: uncached input, cache writes included, plus output;
  cache reads are left out). A rolling minute, so a long session never runs
  dry.
- **Backoff.** ``Backoff``: after a failed call the next waits
  ``BACKOFF_BASE_S``, doubling with each failure in a row up to
  ``BACKOFF_CAP_S``; a good reply resets it.
- **Reply parsing.** :func:`parse_reply` reads the JSON object in a reply,
  fenced or among others, the last one the caller accepts.

Settings (environment variables, defaults in brackets):

- ``AGENTREALM_PLANNER_PROVIDER`` [anthropic, or openai when only an
  OpenAI key is set]: ``anthropic`` or ``openai``.
- ``AGENTREALM_PLANNER_MODEL`` [:data:`DEFAULT_ANTHROPIC_MODEL` for anthropic; required for openai].
- The provider's key: ``AGENTREALM_PLANNER_ANTHROPIC_KEY``, else
  ``ANTHROPIC_API_KEY``; ``AGENTREALM_PLANNER_OPENAI_KEY``, else
  ``OPENAI_API_KEY`` (:data:`KEY_ENV`). The planner's own names come first
  because some hosts strip the standard ones from the environment.
- ``AGENTREALM_PLANNER_EFFORT`` [low]: Anthropic effort level.
- ``AGENTREALM_NO_PLANNER``: set to 1 for the test mode, no planner.

The ``anthropic`` SDK is the planner's one optional dependency (AGENTS.md):
it is imported only when a client for it is made.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4  # estimate for a call that reports no usage
BUDGET_WINDOW_S = 60.0  # the budget counts calls and tokens over this much play
DEFAULT_CALLS_PER_MIN = 6
DEFAULT_TOKENS_PER_MIN = 40_000  # floor; the default also fits one prefix write (default_tokens_per_min)
PREFIX_BUDGET_FACTOR = 1.5
# After a failed call the next waits BACKOFF_BASE_S, doubling per failure in a row, at most BACKOFF_CAP_S.
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 120.0
AUTH_STATUSES = (401, 403)  # the provider refused the planner's key: fatal at startup
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


def env_number(name: str, default: float) -> float:
    """A number from the environment, ``default`` when unset. Raises :class:`PlannerConfigError` on junk."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise PlannerConfigError(f"planner: {name}={raw!r} is not a number") from None


@dataclass(frozen=True)
class ProviderConfig:
    """Which provider, model and key a planner calls, and the effort it asks for."""

    provider: str
    model: str
    api_key: str
    effort: str = DEFAULT_EFFORT

    @classmethod
    def from_env(cls) -> ProviderConfig:
        """Read the provider settings. Raises :class:`PlannerConfigError` when one is missing."""
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
        return cls(provider, model, api_key, os.environ.get("AGENTREALM_PLANNER_EFFORT", "").strip() or DEFAULT_EFFORT)


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


def make_client(cfg: ProviderConfig) -> LLMClient:
    """The provider client for ``cfg``. Add another provider here."""
    if cfg.provider == "anthropic":
        return AnthropicClient(cfg.api_key, cfg.model, cfg.effort)
    return OpenAIChatClient(cfg.api_key, cfg.model)


def connect(cfg: ProviderConfig) -> LLMClient:
    """:func:`make_client`, with a missing SDK as one :class:`PlannerConfigError` line."""
    try:
        return make_client(cfg)
    except ImportError:
        raise PlannerConfigError(
            f"planner: provider {cfg.provider} needs its SDK: run `make setup` (pip install -r python/requirements.txt), or pass --no-planner"
        ) from None


def check_client(client: LLMClient, cfg: ProviderConfig) -> Exception | None:
    """One call before play. Raises :class:`PlannerAuthError` when the
    provider refuses the key (401 or 403), and :class:`PlannerConfigError`
    on any other 4xx but 429 (a typo'd model is a 404). Rate limits, server
    errors and the network are left to the run, which counts them and backs
    off: the error is returned for the caller to log."""
    try:
        client.check()
    except Exception as e:  # network, HTTP status
        status = error_status(e)
        if status in AUTH_STATUSES:
            names = " or ".join(KEY_ENV[cfg.provider])
            raise PlannerAuthError(
                f"planner: {cfg.provider} refused the key (HTTP {status}); fix {names}, or pass --no-planner"
            ) from None
        if status is not None and 400 <= status < 500 and status != 429:
            raise PlannerConfigError(
                f"planner: {cfg.provider} rejected model {cfg.model!r} (HTTP {status}); "
                "fix AGENTREALM_PLANNER_MODEL, or pass --no-planner"
            ) from None
        return e
    return None


# --- the call budget ---


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


def default_tokens_per_min(prefix: str) -> int:
    """The per-minute token budget when none is set: room for one full write
    of the cached ``prefix``, so the first call and a cache miss never silence
    the planner."""
    tokens = estimate_tokens([{"role": "system", "content": prefix}])
    return max(DEFAULT_TOKENS_PER_MIN, math.ceil(PREFIX_BUDGET_FACTOR * tokens))


@dataclass
class CallBudget:
    """Calls and tokens over the last ``BUDGET_WINDOW_S`` of play, every attempt counted.

    A call is charged its estimate when sent (:meth:`charge`), so one that
    fails still uses up the budget, and settled to what the API reported
    when it ends (:meth:`settle`)."""

    spent: deque = field(default_factory=deque)  # [sent_at, tokens] per call in the window

    def limit_reached(self, now: float, calls_per_min: int, tokens_per_min: int) -> str:
        """Which per-minute budget stops a call now, or "" when one may start."""
        while self.spent and now - self.spent[0][0] >= BUDGET_WINDOW_S:
            self.spent.popleft()
        if len(self.spent) >= calls_per_min:
            return "calls_per_min"
        if self.tokens() >= tokens_per_min:
            return "tokens_per_min"
        return ""

    def charge(self, now: float, messages: list[dict[str, Any]]) -> None:
        self.spent.append([now, estimate_tokens(messages)])

    def settle(self, usage: dict[str, Any]) -> None:
        """The last call's real token count, when the API reported one."""
        reported = tokens_used(usage)
        if reported is not None and self.spent:
            self.spent[-1][1] = reported

    def tokens(self) -> int:
        return sum(tokens for _, tokens in self.spent)


@dataclass
class Backoff:
    """After a failed call the next waits ``BACKOFF_BASE_S``, doubling with
    each failure in a row up to ``BACKOFF_CAP_S``; a good reply resets it."""

    failures_in_a_row: int = 0  # failed calls since the last good one
    retry_at: float = 0.0  # after a failure, no call before this clock time

    def waiting(self, now: float) -> bool:
        return now < self.retry_at

    def fail(self, now: float) -> float:
        """Count a failure and return how long the next call waits."""
        self.failures_in_a_row += 1
        delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (self.failures_in_a_row - 1))
        self.retry_at = now + delay
        return delay

    def ok(self) -> None:
        self.failures_in_a_row, self.retry_at = 0, 0.0


# --- reply parsing ---


def json_objects(raw: str) -> list[dict[str, Any]]:
    """Every top-level JSON object in ``raw``, in order, skipping the text between.

    A span is from a ``{`` to the ``}`` that balances it, braces inside strings
    not counted. A span that is not valid JSON is skipped whole, never searched
    for objects nested in it; an unclosed one ends the search.
    """
    found: list[dict[str, Any]] = []
    i = raw.find("{")
    while i >= 0:
        end = _balanced_end(raw, i)
        if end is None:
            break
        try:
            obj = json.loads(raw[i:end])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            found.append(obj)
        i = raw.find("{", end)
    return found


def _balanced_end(raw: str, start: int) -> int | None:
    """The index just past the ``}`` that closes the ``{`` at ``start``, or None."""
    depth, in_string, escaped = 0, False, False
    for j in range(start, len(raw)):
        c = raw[j]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def parse_reply(raw: str, accepts: Callable[[dict[str, Any]], bool]) -> Any:
    """The JSON object in a reply; a model without a JSON mode may wrap it in a code fence.

    A reply holding several objects (an answer, then a correction) gives the
    last one ``accepts`` takes: the planner's final word. Raises
    ``ValueError`` when no JSON can be read.
    """
    try:
        return json.loads(raw)
    except ValueError:
        objects = json_objects(raw)
        accepted = [obj for obj in objects if accepts(obj)]
        if len(objects) > 1:
            log.info("planner reply held %d JSON objects, %d accepted; using the last accepted", len(objects), len(accepted))
        if accepted:
            return accepted[-1]
        first, last = raw.find("{"), raw.rfind("}")
        if first < 0 or last < first:
            raise
        return json.loads(raw[first : last + 1])
