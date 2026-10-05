"""What the live acceptance runs (M6 A4, M7 A16, M9 A29) share.

``AcceptanceHooks`` are the hooks the runner calls on an attached acceptance
object; each does nothing here, so a metrics class overrides only the hooks it
measures. ``CountingClient`` records failed requests (and optionally every
call).
"""

from __future__ import annotations

import time

from .config import Policy
from .client import ApiError
from .executor.intents import wait
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

    def on_oscillation(self, event: dict) -> None:
        """The dispatch guard caught the character pacing between two cells (A15)."""


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


# Self reads before giving up on a character that stays asleep or downed, one
# a second: well inside the call budget, and longer than the 5 s respawn delay.
WAKE_READS = 30
# What to do about the known wake rejections (GAME_NOTES Sleep). Any other
# rejected wake ``Wait`` also stops the start, with its code.
WAKE_REFUSALS = {
    "alive_cap_full": "the world's alive cap is full; wait for a slot and re-run",
    "block_occupied": "no free block to wake on; wait and re-run",
}


def wake(client, cid: int, *, pause=time.sleep) -> None:
    """Get the character awake and alive before its position is read.

    A sleeping character (asleep after 10 idle minutes) is off the map and
    has no position. Any intent wakes it; ``Wait`` is the one that does
    nothing else (GAME_NOTES Sleep). A downed one respawns after
    ``respawn_delay_seconds``, so this reads self until it is alive.
    Raises ValueError saying why it could not.

    This repeats Sync's wake (``states/sync.py``) because the smoke scripts
    read a position before the runner starts; keep the two in step.
    """
    for _ in range(WAKE_READS):
        s = client.self_(cid)
        if not s.get("alive", True):
            pause(1.0)
            continue
        if not s.get("asleep"):
            return
        reply = client.tick(cid, [wait()])
        for result in reply.get("intent_results") or []:
            if result.get("outcome") == "rejected":
                code = (result.get("rejection") or {}).get("code") or "unknown"
                raise ValueError(f"cannot wake: {code}: {WAKE_REFUSALS.get(code, 'wake Wait rejected')}")
        pause(1.0)
    raise ValueError(f"still asleep or downed after {WAKE_READS} self reads")


def navigation_start(client, cid: int) -> tuple[int, tuple[int, int]]:
    """The overworld's map id and where the character stands on it.

    Raises ValueError when the character is not on the overworld, where the
    M7 and M9 gates are judged.
    """
    town = client.world(cid).get("town") or {}
    p = client.position(cid)
    if town.get("map_id") is None or p.get("map_id") != town["map_id"]:
        raise ValueError(f"character is on map {p.get('map_id')}, not the overworld {town.get('map_id')}")
    return int(town["map_id"]), (int(p["x"]), int(p["y"]))
