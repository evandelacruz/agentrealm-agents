"""Shared Olympuff smoke helpers (M7 A16, M11 A40)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import TypeVar

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.client import Client  # noqa: E402
from agentrealm_agent.executor.intents import wait  # noqa: E402
from agentrealm_agent.knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.m7_acceptance import M7AcceptanceMetrics  # noqa: E402
from agentrealm_agent.m11_acceptance import M11AcceptanceMetrics  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402

GateMetrics = TypeVar("GateMetrics", M7AcceptanceMetrics, M11AcceptanceMetrics)

DEFAULT_BASE = "https://api.agentrealm.gg"

# Self reads before giving up on a character that stays asleep or downed, one
# a second: well inside the call budget, and longer than the 5 s respawn delay.
WAKE_READS = 30
# What to do about the known wake rejections (GAME_NOTES Sleep). Any other
# rejected wake ``Wait`` also stops the start, with its code.
WAKE_REFUSALS = {
    "alive_cap_full": "the world's alive cap is full; wait for a slot and re-run",
    "block_occupied": "no free block to wake on; wait and re-run",
}


def wake(client: Client, cid: int, *, pause=time.sleep) -> None:
    """Get the character awake and alive before its position is read.

    A sleeping character (asleep after 10 idle minutes) is off the map and
    has no position. Any intent wakes it; ``Wait`` is the one that does
    nothing else (GAME_NOTES Sleep). A downed one respawns after
    ``respawn_delay_seconds``, so this reads self until it is alive.
    Raises ValueError saying why it could not.

    This repeats Sync's wake (``states/sync.py``) because the smoke scripts
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


def navigation_start(client: Client, cid: int) -> tuple[int, tuple[int, int]]:
    """The overworld's map id and where the character stands on it.

    Raises ValueError when the character is not on the overworld: M7 and M11 start there.
    """
    town = client.world(cid).get("town") or {}
    p = client.position(cid)
    if town.get("map_id") is None or p.get("map_id") != town["map_id"]:
        raise ValueError(f"character is on map {p.get('map_id')}, not the overworld {town.get('map_id')}")
    return int(town["map_id"]), (int(p["x"]), int(p["y"]))


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: GateMetrics,
    *,
    timeout_s: float,
) -> tuple[GateMetrics, float]:
    stop = threading.Event()
    metrics.stop = stop
    started = time.monotonic()
    knowledge: KnowledgeBase = load_knowledge(cfg.world)

    def out(line: str) -> None:
        print(line, flush=True)

    runner = Runner(
        cfg,
        metrics.wrap(client),
        cid,
        stop,
        out,
        knowledge=knowledge,
        acceptance=metrics,
    )

    def watchdog() -> None:
        if timeout_s <= 0:
            return
        if stop.wait(timeout_s):
            return
        out(f"[{cfg.profile}] timeout after {timeout_s:.0f}s")
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
        out(f"knowledge base {knowledge.world_code}: not saved: {e}")
    return metrics, elapsed
