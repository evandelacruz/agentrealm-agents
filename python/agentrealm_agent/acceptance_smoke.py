"""Shared helpers for live acceptance smoke scripts (M7 A16, M10 A33)."""

from __future__ import annotations

import threading
import time
from typing import Callable

from . import config
from .acceptance import AcceptanceHooks
from .client import Client
from .executor.intents import wait
from .knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge
from .runner import Runner

# Self reads before giving up on a character that stays asleep or downed, one
# a second: well inside the call budget, and longer than the 5 s respawn delay.
WAKE_READS = 30
# What to do about the known wake rejections (GAME_NOTES Sleep). Any other
# rejected wake ``Wait`` also stops the start, with its code.
WAKE_REFUSALS = {
    "alive_cap_full": "the world's alive cap is full; wait for a slot and re-run",
    "block_occupied": "no free block to wake on; wait and re-run",
}


def wake(client: Client, cid: int, *, pause: Callable[[float], None] = time.sleep) -> None:
    """Get the character awake and alive before its position is read.

    A sleeping character (asleep after 10 idle minutes) is off the map and
    has no position. Any intent wakes it; ``Wait`` is the one that does
    nothing else (GAME_NOTES Sleep). A downed one respawns after
    ``respawn_delay_seconds``, so this reads self until it is alive.
    Raises ValueError saying why it could not.

    This repeats Sync's wake (``states/sync.py``) because acceptance scripts
    need a position before the runner starts; keep the two in step.
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


def run_acceptance_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: AcceptanceHooks,
    *,
    timeout_s: float,
    out: Callable[[str], None] | None = None,
) -> tuple[float, KnowledgeBase]:
    """Run the runner with ``metrics`` until it stops or ``timeout_s`` elapses.

    Returns the seconds played and the knowledge base the runner wrote to; judge
    the run on that one, not a reload, which misses the run if the save failed.
    """
    stop = threading.Event()
    metrics.stop = stop
    started = time.monotonic()
    knowledge: KnowledgeBase = load_knowledge(cfg.world)

    def emit(line: str) -> None:
        if out is not None:
            out(line)

    runner = Runner(
        cfg,
        metrics.wrap(client),
        cid,
        stop,
        emit,
        knowledge=knowledge,
        acceptance=metrics,
    )

    def watchdog() -> None:
        if timeout_s <= 0:
            return
        if stop.wait(timeout_s):
            return
        emit(f"[{cfg.profile}] timeout after {timeout_s:.0f}s")
        stop.set()

    thread = threading.Thread(target=runner.run, daemon=True)
    wd = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    wd.start()
    thread.join()
    stop.set()
    elapsed = time.monotonic() - started
    try:
        save_knowledge(knowledge)
    except OSError as e:
        emit(f"knowledge base {knowledge.world_code}: not saved: {e}")
    return elapsed, knowledge
