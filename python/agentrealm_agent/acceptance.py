"""What the live acceptance runs (M6 A4, M7 A16) share.

``AcceptanceHooks`` are the hooks the runner calls on an attached acceptance
object; each does nothing here, so a metrics class overrides only the hooks it
measures. ``CountingClient`` records failed requests (and optionally every
call), and ``ensure_character`` finds or creates the smoke script's character.
"""

from __future__ import annotations

from . import config
from .client import ApiError
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import WorldModel


class AcceptanceHooks:
    """No-op base for acceptance metrics. Override the hooks you need."""

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
    ) -> None:
        """A tick is about to be sent. ``w`` is the world the decision saw.

        ``intents`` is None when a held queue keeps running and nothing new is sent.
        """

    def on_step_applied(self) -> None:
        """One of our Steps applied."""

    def on_rejection(self, code: str, *, verb: str | None = None) -> None:
        """One of our intents was rejected with ``code``."""

    def on_death(self) -> None:
        """A ``Died`` event arrived for our character."""


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


def _find_character_id(client, cfg: config.CharacterConfig) -> int | None:
    # GET /characters: {"characters": [{"id", "name", "world_code", ...}]}
    # (API, Characters; https://agentrealm.gg/docs/api).
    for row in client.list_characters():
        if row.get("name") == cfg.name and row.get("world_code") == cfg.world:
            return int(row["id"])
    return None


def ensure_character(client, cfg: config.CharacterConfig) -> int:
    """The character id from ``.state``, else a new character, saved to ``.state``."""
    state = config.load_state(cfg)
    if state is not None:
        return int(state["character_id"])
    try:
        created = client.create_character(cfg.world, cfg.name, cfg.avatar, cfg.model_agent)
    except ApiError as e:
        # 409 identity_reuse: this name already lived in this world, so a lost
        # .state file is the likely cause. character_cap_reached: the account
        # is full, but the character may still be one of its own. Look it up
        # by name; any other failure, or no match, is real.
        if e.code not in ("identity_reuse", "character_cap_reached"):
            raise
        found = _find_character_id(client, cfg)
        if found is None:
            raise
        config.save_state(cfg, {"character_id": found, "world": cfg.world})
        return found
    config.save_state(cfg, {"character_id": created["id"], "world": cfg.world})
    return int(created["id"])
