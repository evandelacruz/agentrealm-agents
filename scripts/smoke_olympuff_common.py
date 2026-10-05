"""Shared Olympuff smoke helpers (M7 A16, M11 A40)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.acceptance import AcceptanceHooks  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.executor.intents import wait  # noqa: E402
from agentrealm_agent.knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402

DEFAULT_BASE = "https://api.agentrealm.gg"

WAKE_READS = 30
WAKE_REFUSALS = {
    "alive_cap_full": "the world's alive cap is full; wait for a slot and re-run",
    "block_occupied": "no free block to wake on; wait and re-run",
}


def wake(client: Client, cid: int, *, pause=time.sleep) -> None:
    """Get the character awake and alive before its position is read."""
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
    """The overworld's map id and where the character stands on it."""
    town = client.world(cid).get("town") or {}
    p = client.position(cid)
    if town.get("map_id") is None or p.get("map_id") != town["map_id"]:
        raise ValueError(f"character is on map {p.get('map_id')}, not the overworld {town.get('map_id')}")
    return int(town["map_id"]), (int(p["x"]), int(p["y"]))


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: AcceptanceHooks,
    *,
    timeout_s: float,
) -> tuple[AcceptanceHooks, float]:
    stop = threading.Event()
    if hasattr(metrics, "stop"):
        metrics.stop = stop  # type: ignore[attr-defined]
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
