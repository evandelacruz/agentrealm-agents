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
from .travel.resolve import travel_dest, travel_given_up
from .travel.strength import StrengthBracket
from .knowledge_base import KnowledgeBase, knowledge_items
from .memory import Memory
from .navigation.stuck import HUB_GIVE_UP_CELLS, NavStuckMemory, hub_give_up_lapses
from .gem_yield import summary as gem_yield_summary
from .planner_reference import game_notes_text, reference_text
from .plan import OP_FIELDS, MAX_WAIT_SECONDS, PARAM_MEANINGS, Plan, collect_rejections, parse_plan_payload
from .investigation import HELPER_STILL_TICKS, greeted_npc_ids, in_sight, spoken_npc_ids
from .survival import known_hostile, retreat_goal
from .travel.knowledge import iter_entrances, town_from_kb
from .travel.ops import travel_op_from_plan_goal
from .world import Pos, WorldModel, chebyshev
from .zone_discovery import known_safe

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
STALL_SECONDS = 30  # State shows a stall once nothing has changed for this long
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


def _param_table() -> str:
    return "\n".join(f"- {name}: {meaning}" for name, meaning in PARAM_MEANINGS.items())


SYSTEM_PROMPT = f"""You are the planner for an Agent Realm character. Plan from the game reference above: it is the game's own documentation of its rules, intents, combat, survival, items and maps. Where it and the measured facts disagree, trust the measured facts. You own its goal stack: the states work on the op on top. With an empty stack you are not steering: the dispatcher's safe default runs (exploring in safe ground).

Reply with one JSON object only, no markdown, with these keys:
- "goals": the whole new goal stack, top first. Omit the key to keep the current stack. An empty or invalid list clears it.
- "params": survival params to change, starting from the current values under State; omit it to keep them
- "notes": optional string for the trace

Each goal is an object with "op" and that op's fields; every op may also carry "why". These are the only ops (anything else is dropped):
{_op_table()}

State lists the current stack, each op marked "pinned" or "planner". Pinned ops come from the directives file (the user's manual steering, or the run's own target). You cannot remove, reorder or replace them: whatever you send, they stay on top, until they are done or stuck detection gives up on their target. Plan around them. Your "goals" are only your own part of the stack, the ops below the pinned ones; leave pinned ops out of it. Never send a travel, of any kind, whose destination is a cell listed under given_up_travel: stuck detection gave up on it. A town or shop cell there is also under given_up_hubs, with when that give-up lapses: at retry_at_tick, or once the character stands or_after_moving_cells cells from where it gave up (from); it then leaves both lists and may be travelled to again. Any other listed cell is given up for the rest of the run.

The survival params ("params" under State, set by "params" or a set_param op). Survival params may only tighten past params_floor; a change that loosens one is ignored:
{_param_table()}

Safe ground: hostiles cannot hurt the character only while it stands on safe ground. State safe_ground says whether it does now, and nearest_safe and town say how far away (Chebyshev cells) and which way those are. Away from safe ground, a wait or any op that stays put leaves a hurt character exposed: travel to town or let Retreat walk to the nearest safe tile first.

NPCs: State nearby_npcs lists the nearest NPCs in sight (id, type, cells and dir from here, spoken, greeted, hostile, stays_put); npcs_spoken_to counts the NPCs a say op of yours has spoken to so far, and npcs_greeted those Greet has said hello to. The game does not say which NPCs are helpers and which are monsters. "hostile" true means known hostile: a boss, the last thing that hit the character, or a type that has swung at it, hit it or died in view; false only means none of its type has done so yet, so it may still be a monster. "stays_put" true means it has stood on one cell for a while, as helpers do; judge the rest from its type and the clues. On safe ground (anywhere, if policy.hostile does not name NPCs), Greet says hello once to an NPC in sight that stays put and is not hostile, and marks it greeted; a helper answers any words with the same line, which lands in Clues. A greeting never counts as spoken: a say op to a greeted NPC still says your text. To talk to one further away, or with your own words, use a say op with its npc_id (from nearby_npcs) or its npc_type (any NPC of that type, the nearest first); the character walks within speech range and says your text. A helper's reply is added to Clues as a row of kind "npc".

A travel with no x, y (town, hunting_ground, a nearest shop or entrance) shows in the stack without them, with "goes_to": the cell it walks to now, which the agent works out itself. It is already on the stack: re-sending it changes nothing.

State stall shows how long the character has neither moved, gained or spent gems, gained or lost an item, nor cleared a level, once that passes {STALL_SECONDS} s, and the decision it last made: the stack is not working, so change it. level_entrances lists the known level entrances nearest first (travel to one with to "entrance", its x, y and map_id). shop_prices lists the gem price of every item seen for sale, and which ones the gems held can buy; a buy op takes only the item it names.

When State shows last_reply_rejected, those parts of your previous reply were dropped or ignored, for the reasons given; the rest of it was applied. Do not repeat them unchanged.

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

Gems by area (stage 2). Gem drops from grass and bushes vary by area, and some areas drop none. State gem_yield is measured from the character's own cuts: the region it stands in (here, once cut there), the best regions at any distance with their yield (gems per cut) and distance in blocks, and the barren ones nearby. A good region far behind is still worth a travel back. Hunt gems where the yield is good, leave a region that shows no gems after a fair sample, and explore regions not yet sampled to sample them.

How gather_gems works. Gather cuts known grass and bushes off hazards with no hostile near (a hostile that has not hit us only bars cells within weapon reach plus a step; one that shadows for 15 s without attacking is fought when the profile fights and would win, else Gather walks well off from it), field cells before safe-zone ones (gems drop from cuts outside town, and a cut on town grass was seen to have no effect). It walks to the nearest one itself, learns ground where cuts have no effect and leaves it, and with nothing left to cut while on safe ground it heads out to field ground or the frontier; the survival states keep the character alive while it does. It skips barren regions. Once its region shows a poor yield after a fair sample (20 cuts, under 1 gem in 20), it leaves poor regions alone and walks to the best region within 64 blocks that gave at least 1 gem in 10, and cuts there. A gather_gems x, y names a target region (any block of it, such as a gem_yield corner): Gather walks there and cuts only there while it knows a cell to cut there, and works as usual once it knows none; it also lifts that region's barren mark. Name one to send Gather to a good region it would not pick itself (a far one, say); leave x, y out to let it choose, and never re-send an otherwise unchanged gather_gems just to change x, y. A gather_gems of yours under a pinned gather_gems with no higher count is a repeat of it and is dropped. State gather_status, shown while a gather_gems is on top, is Gather's last decision: "cutting" (a cut sent now), "taking a gem", "walking to grass", "walking to a bush" or "walking to a gem pile", "moving off from a hostile that shadows", "fighting a hostile that shadows", "blocked by hostile" (the only cuttable cells known have a hostile near), "heading out of safe ground" (nothing left to cut, walking out of the safe zone), "cuts have no effect here" (cuts here changed nothing, so it moves on to other cells), "no cuttable cell in view" (it knows no grass or bush it may cut, so it explores for one), or "region barren" (the same, standing in a barren region); "walking to a target region" while it heads for a named region it has not seen. While it works only in a target region, the status ends in " (region x,y)", that region's corner. Any of these but "cutting" ends in ", no cut for N s" once no cut has taken effect for 30 s: that is a stall, not progress. State gather_run counts this run's cuts that took effect (cuts), cuts that did nothing (no_effect_cuts) and gems the counter gained (gems_gained): cuts rising with gems_gained flat for long is a stall, not progress."""


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


def hub_give_up_lines(stuck: NavStuckMemory) -> list[dict[str, Any]]:
    """The planner's ``given_up_hubs``: each town or shop give-up and when it
    lapses (``stuck.HUB_GIVE_UP_TICKS``, ``stuck.HUB_GIVE_UP_CELLS``)."""
    out = []
    for mid, (x, y) in sorted(stuck.given_up_hubs, key=str):
        lapse = hub_give_up_lapses(stuck, (mid, (x, y)))
        if lapse is None:
            continue
        tick, start = lapse
        entry: dict[str, Any] = {"cell": f"{mid}:{x},{y}", "retry_at_tick": tick, "or_after_moving_cells": HUB_GIVE_UP_CELLS}
        if start is not None:
            entry["from"] = f"{start[0]},{start[1]}"
        out.append(entry)
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
    given_up_hubs: Collection[dict[str, Any]] = (),
    gather_status: str = "",
    gather_run: dict[str, int] | None = None,
    rejected: Collection[str] = (),
    stall: str = "",
    strength: StrengthBracket | None = None,
) -> list[dict[str, Any]]:
    """The model's input: the cached system prefix (:func:`system_prompt`), then
    one user message with triggers, state, the remaining plan, every clue, and instructions."""
    pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos and w.map_id is not None else "unknown"
    state_lines = [
        f"tick={w.tick} pos={pos} alive={w.alive} health={w.health}/{w.max_health} gems={w.gems}",
        f"map_level={w.map_level} armed={w.armed_code} lives={w.lives}",
        f"worn={json.dumps(w.worn_codes, sort_keys=True)} held={json.dumps(dict(sorted(Counter(s.code for s in w.held_supplies).items())))}",
        f"levels_cleared={w.levels_cleared} level_count={w.level_count}",
        *safety_lines(w, knowledge),
        *npc_lines(w, knowledge),
        f"gem_yield={json.dumps(gem_yield_summary(w, knowledge), sort_keys=True)}",
        *_gather_line(plan, gather_status),
        f"gather_run={json.dumps(gather_run or {}, sort_keys=True)}",
        f"stall={stall or 'none'}",
        *entrance_lines(w, knowledge),
        shop_price_line(w, knowledge),
        f"params={json.dumps(plan.params, sort_keys=True)}",
        f"params_floor={json.dumps(directives.params, sort_keys=True)} (survival params may only tighten past these)",
    ]
    if plan.notes:
        state_lines.append(f"plan_notes={plan.notes!r}")
    bracket = strength or StrengthBracket()
    lines = stack_lines(plan, lambda op: travel_dest(op, w, knowledge, bracket, given_up_travel))
    state_lines.append("stack (top first):" + "".join(f"\n  {line}" for line in lines) if lines else "stack: (empty)")
    if given_up_travel:
        cells = [f"{mid}:{x},{y}" for mid, (x, y) in sorted(given_up_travel, key=str)]
        state_lines.append(f"given_up_travel={json.dumps(cells)} (cells stuck detection gave up on: never travel to them while listed)")
    if given_up_hubs:
        state_lines.append(f"given_up_hubs={json.dumps(list(given_up_hubs), sort_keys=True)} (town and shop give-ups: each lapses at retry_at_tick or after moving or_after_moving_cells from where it gave up)")
    if rejected:
        state_lines.append(f"last_reply_rejected={json.dumps(list(rejected))} (parts of your previous reply that were not applied, and why)")
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


def safety_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """Whether the character stands on safe ground, and how far and which
    way the nearest known safe tile and the town are. ``yes`` once a zone or
    terrain read shows the cell safe; ``unknown`` until a zone read covers a
    cell no terrain read marked safe."""
    if w.pos is None or w.map_id is None:
        return []
    fact = w.zones.get(w.map_id, {}).get(w.pos)
    if known_safe(w, w.map_id, w.pos):
        here = "yes"
    else:
        here = "unknown (zone not read here)" if fact is None else "no"
    safe = retreat_goal(w, None)  # safe tiles only; the town line follows
    town = town_from_kb(knowledge) or (w.respawn_anchors[0] if w.respawn_anchors else None)
    return [
        f"safe_ground={here}",
        f"nearest_safe={_bearing(w, (w.map_id, safe)) if safe is not None else 'none known on this map'}",
        f"town={_bearing(w, town) if town is not None else 'unknown'}",
    ]


# The nearest NPCs in sight that State lists (A65).
NEARBY_NPCS_SHOWN = 5


def npc_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """The nearest NPCs in sight, and how many were spoken to and greeted so far (A65).

    Each NPC: ``id``, ``type``, ``cells`` (Chebyshev) and ``dir`` from here,
    ``spoken`` (a ``say`` op's text was said to it, ``spoken_npcs``),
    ``greeted`` (Greet's hello was, ``greeted_npcs``), ``hostile`` (``survival.known_hostile``:
    a boss, our last hitter, or a type that has swung at us, hit us or died in view) and ``stays_put`` (on
    one cell for ``HELPER_STILL_TICKS`` in view, as helpers do). The API names
    no helpers, so these are the only hints.
    """
    spoken, greeted = spoken_npc_ids(knowledge), greeted_npc_ids(knowledge)
    lines = [f"npcs_spoken_to={len(spoken)} npcs_greeted={len(greeted)}"]
    if w.pos is None or w.map_id is None:
        return lines
    here, map_id = w.pos, w.map_id
    npcs = sorted(
        (e for e in w.entities if e.kind == "npc" and in_sight(w, map_id, here, e.pos)),
        key=lambda e: (chebyshev(e.pos, here), e.id),
    )[:NEARBY_NPCS_SHOWN]
    rows = [
        {
            "id": e.id,
            "type": e.code or None,
            "cells": chebyshev(e.pos, here),
            "dir": compass(here, e.pos) or "here",
            "spoken": e.id in spoken,
            "greeted": e.id in greeted,
            "hostile": known_hostile(w, e),
            "stays_put": w.npc_still_ticks(e) >= HELPER_STILL_TICKS,
        }
        for e in npcs
    ]
    lines.append(f"nearby_npcs={json.dumps(rows, sort_keys=True)}")
    return lines


def _bearing(w: WorldModel, where: tuple[int, Pos]) -> str:
    """``map:x,y (N cells <direction>)``, or ``here``; another map gives no distance."""
    map_id, pos = where
    cell = f"{map_id}:{pos[0]},{pos[1]}"
    if map_id != w.map_id or w.pos is None:
        return f"{cell} (another map)"
    if pos == w.pos:
        return f"{cell} (here)"
    return f"{cell} ({chebyshev(w.pos, pos)} cells {compass(w.pos, pos)})"


def compass(a: Pos, b: Pos) -> str:
    """Which way ``b`` lies from ``a``, eight-way; y grows southward."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    # A component under half the other counts as none: (10, 3) is east.
    ns = "north" if dy < 0 else "south" if dy > 0 else ""
    ew = "west" if dx < 0 else "east" if dx > 0 else ""
    if abs(dy) * 2 < abs(dx):
        ns = ""
    if abs(dx) * 2 < abs(dy):
        ew = ""
    return f"{ns}-{ew}" if ns and ew else ns or ew


def _gather_line(plan: Plan, gather_status: str) -> list[str]:
    """``gather_status``, while a ``gather_gems`` is on top and Gather has decided once."""
    head = plan.current()
    if head is None or head["op"] != "gather_gems" or not gather_status:
        return []
    return [f"gather_status={json.dumps(gather_status)}"]


def repeats_pinned(op: dict[str, Any], pinned: list[dict[str, Any]]) -> bool:
    """``op`` adds nothing below the pinned ops: it is one of them (``why``
    aside), or a ``gather_gems`` with no higher count than a pinned one. That
    one is done on reaching the top (gems at its count), so its x, y never act."""
    for p in pinned:
        if same_ops([op], [p]):
            return True
        if op["op"] == p["op"] == "gather_gems" and op["count"] <= p["count"]:
            return True
    return False


def stack_lines(plan: Plan, goes_to: Callable[[dict[str, Any]], tuple[int, Pos] | None] | None = None) -> list[str]:
    """The ops left on the stack, top first, each marked ``pinned`` (a
    directives op, which the planner cannot remove) or ``planner`` (A35).
    Each op as :func:`shown_op` shows it, with ``goes_to`` for a symbolic travel."""
    return [
        f"{'pinned' if i < plan.directive_end else 'planner'} {json.dumps(shown_op(op, goes_to), sort_keys=True)}"
        for i, op in enumerate(plan.goals[plan.index :], plan.index)
    ]


def shown_op(op: dict[str, Any], goes_to: Callable[[dict[str, Any]], tuple[int, Pos] | None] | None = None) -> dict[str, Any]:
    """``op`` as State shows it. A travel the agent finds the cell for
    (``travel_op_from_plan_goal`` reads no x, y) drops the ``0, 0`` placeholder
    validation gave it, which reads like the map origin and invited re-sends
    (free-play run 1), and says where it ``goes_to`` now instead."""
    if op.get("op") != "travel" or travel_op_from_plan_goal(op).x is not None:
        return op
    shown = {k: v for k, v in op.items() if k not in ("x", "y")}
    dest = goes_to(op) if goes_to is not None else None
    if dest is not None:
        shown["goes_to"] = f"{dest[0]}:{dest[1][0]},{dest[1][1]}"
    else:
        shown["goes_to"] = "nearest unexplored door" if op["to"] == "entrance" else "not known yet"
    return shown


# Level entrances State lists, nearest first.
ENTRANCES_SHOWN = 8


def entrance_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """The known level entrances (``kb.entrances``), nearest first: cell,
    bearing, and what a look at it filed (``block_type``, ``locked``, ``needs``)."""
    rows = []
    for map_id, pos, row in iter_entrances(knowledge):
        entry: dict[str, Any] = {"cell": _bearing(w, (map_id, pos))}
        for key in ("block_type", "locked", "needs"):
            if row.get(key) is not None:
                entry[key] = row[key]
        here = w.map_id == map_id and w.pos is not None
        rows.append(((0 if here else 1, chebyshev(w.pos, pos) if here else 0, map_id, pos), entry))
    if not rows:
        return ["level_entrances=none known"]
    rows.sort(key=lambda t: t[0])
    shown = [entry for _, entry in rows[:ENTRANCES_SHOWN]]
    more = f" (+{len(rows) - len(shown)} more)" if len(rows) > len(shown) else ""
    return [f"level_entrances={json.dumps(shown, sort_keys=True)}{more}"]


def shop_price_line(w: WorldModel, knowledge: KnowledgeBase | None) -> str:
    """Every item seen for sale (``items`` rows with a ``gem_price``, and
    priced supplies in sight), cheapest first, against the gems held."""
    prices: dict[str, int] = {}
    for code, row in knowledge_items(knowledge).items():
        price = row.get("gem_price") if isinstance(row, dict) else None
        if isinstance(price, int) and price > 0:
            prices[code] = price
    for e in w.entities:
        if e.kind == "supply" and e.code and isinstance(e.gem_price, int) and e.gem_price > 0:
            prices[e.code] = e.gem_price
    if not prices:
        return "shop_prices=none seen"
    ordered = dict(sorted(prices.items(), key=lambda t: (t[1], t[0])))
    gems = w.gems or 0
    affordable = [code for code, price in ordered.items() if price <= gems]
    return f"shop_prices={json.dumps(ordered)} gems={gems} can_buy_now={json.dumps(affordable)}"



@dataclass
class StallClock:
    """When the character last moved, gained or spent gems, gained or lost
    an item or cleared a level: progress the planner can see (free-play run 1
    stood still for six minutes with the stack unchanged)."""

    key: tuple | None = None
    since: int = 0

    def note(self, w: WorldModel) -> None:
        # What is owned, not what is armed: an arm flip-flop is no progress.
        owned = sorted([s.code for s in w.held_supplies] + [w.armed_code or ""] + list(w.worn_codes.values()))
        key = (w.map_id, w.pos, w.gems, tuple(c for c in owned if c), tuple(w.levels_cleared or ()))
        if key != self.key:
            self.key, self.since = key, w.tick

    def line(self, w: WorldModel, tick_hz: int, reason: str) -> str:
        """``""`` until ``STALL_SECONDS`` pass with no change, then how long and the last decision."""
        seconds = (w.tick - self.since) // max(1, tick_hz)
        if self.key is None or seconds < STALL_SECONDS:
            return ""
        return json.dumps({"seconds": seconds, "last_decision": reason or "unknown"}, sort_keys=True)


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
    rejected: list[str] = field(default_factory=list)  # what the last reply had dropped or ignored, for the next State
    stall: StallClock = field(default_factory=StallClock)
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
        self.stall.note(w)
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
        if since < 0 and runner.server_tick is not None:
            # The idle clock starts at the first tick the server reported, not at 0.
            since = m.strategist_progress_tick = w.tick
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
            given_up_hubs=hub_give_up_lines(runner.mem.nav_stuck),
            gather_status=runner.mem.gather_status,
            gather_run=runner.gem_cuts.run_counts(),
            rejected=self.rejected,  # replaced when this call's reply is read; kept if the call fails
            stall=self.stall.line(runner.world, runner.tick_hz, runner.mem.last_decision),
            strength=runner.mem.strength,
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
        left stay on top, and the planner's goals go below them (A35), less any
        that only repeats one (:func:`repeats_pinned`).
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
        (goals, params, notes), self.rejected = collect_rejections(
            lambda: parse_plan_payload(reply, floor_params=dict(d.params), current_params=runner.plan.params)
        )
        if self.rejected:
            record["rejected"] = self.rejected
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
            def gave_up(g):
                return travel_given_up(g, runner.world, runner.knowledge, runner.mem.strength, given_up)

            kept = [g for g in goals if not gave_up(g)]
            if len(kept) < len(goals):
                record["given_up_filtered"] = [g for g in goals if g not in kept]
                goals = kept
        old = runner.plan
        pinned = old.directive_ops()  # directives ops left: they stay on top
        # A reply that repeats a directives op does not stack it twice, so a
        # reply that differs only in such a repeat leaves the stack unchanged.
        repeats = [g for g in goals if repeats_pinned(g, pinned)]
        if repeats:
            record["pinned_repeats"] = repeats
        goals = pinned + [g for g in goals if g not in repeats]
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
            runner.mem.gather_status = ""  # it was the old head's
        if not goals:
            runner.log("strategist", "no valid goals; stack cleared (dispatcher safe default)", {"strategist": {"event": "cleared", **record}})
            return
        below = f", under {len(pinned)} directives op(s)" if pinned else ""
        runner.log("strategist", f"plan replaced ({len(goals) - len(pinned)} goals{below})", {"strategist": {"event": "applied", **record}})
        if runner.acceptance is not None:
            runner.acceptance.on_strategist_applied(goals)
