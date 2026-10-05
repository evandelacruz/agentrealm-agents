#!/usr/bin/env python3
"""A16: Live M7 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M7 done-when).

Plays one hour (default) from wherever the character stands on the overworld.
Before the runner starts it reads the world and the character's position, and
sends the agent to one ``goto`` target 150 blocks east of that start (or
``--target X,Y``); the rest of the hour it explores. Pass criteria are in
``agentrealm_agent/m7_acceptance.py`` and the README. Requires AGENTREALM_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.m7_acceptance import TARGET_DISTANCE, TARGET_SECONDS, M7AcceptanceMetrics  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402

DEFAULT_CHARACTER = PYTHON / "characters" / "olympuff_m7.toml"
DEFAULT_BASE = "https://api.agentrealm.gg"


def _find_character_id(client: Client, cfg: config.CharacterConfig) -> int | None:
    for row in client.list_characters():
        if row.get("name") == cfg.name and row.get("world_code") == cfg.world:
            return int(row["id"])
    return None


def ensure_character(client: Client, cfg: config.CharacterConfig) -> int:
    state = config.load_state(cfg)
    if state is not None:
        return int(state["character_id"])
    try:
        created = client.create_character(cfg.world, cfg.name, cfg.avatar, cfg.model_agent)
    except ApiError as e:
        if e.code not in ("identity_reuse", "character_cap_reached"):
            raise
        found = _find_character_id(client, cfg)
        if found is None:
            raise
        config.save_state(cfg, {"character_id": found, "world": cfg.world})
        return found
    config.save_state(cfg, {"character_id": created["id"], "world": cfg.world})
    return int(created["id"])


def navigation_start(client: Client, cid: int) -> tuple[int, tuple[int, int]]:
    """The overworld's map id and where the character stands on it.

    Raises ValueError when the character is not on the overworld: M7 is judged there.
    """
    town = client.world(cid).get("town") or {}
    p = client.position(cid)
    if town.get("map_id") is None or p.get("map_id") != town["map_id"]:
        raise ValueError(f"character is on map {p.get('map_id')}, not the overworld {town.get('map_id')}")
    return int(town["map_id"]), (int(p["x"]), int(p["y"]))


def aim_at(cfg: config.CharacterConfig, overworld: int, target: tuple[int, int]) -> None:
    """Send the agent to ``target`` first, then let it explore for the rest of the hour."""
    cfg.policy.goto, cfg.policy.goto_map = target, overworld
    cfg.policy.goals = ["goto"] + [g for g in cfg.policy.goals if g != "goto"]


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: M7AcceptanceMetrics,
    *,
    timeout_s: float,
) -> tuple[M7AcceptanceMetrics, float]:
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
        out(f"[{cfg.name}] timeout after {timeout_s:.0f}s")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M7 acceptance smoke test on Olympuff (A16).")
    ap.add_argument(
        "--character",
        type=Path,
        default=DEFAULT_CHARACTER,
        help="character TOML (default: python/characters/olympuff_m7.toml)",
    )
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument(
        "--seconds",
        type=float,
        default=TARGET_SECONDS,
        help="wall-clock seconds to play before stopping (M7 done-when: 3600)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=TARGET_SECONDS + 600.0,
        help="hard timeout seconds (0 = no limit)",
    )
    ap.add_argument(
        "--target",
        default="",
        help=f"overworld goto target X,Y (default: {TARGET_DISTANCE} blocks east of the start)",
    )
    args = ap.parse_args(argv)

    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    try:
        cfg = config.load(args.character)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if cfg.world != "olympuff":
        print(f"expected world olympuff, got {cfg.world!r}", file=sys.stderr)
        return 2

    client = Client(args.base_url, args.api_key)
    try:
        cid = ensure_character(client, cfg)
    except ApiError as e:
        print(f"create: {e}", file=sys.stderr)
        return 2

    try:
        overworld, origin = navigation_start(client, cid)
    except (ApiError, ValueError) as e:
        print(f"start: {e}", file=sys.stderr)
        return 2
    if args.target:
        x, y = (int(v) for v in args.target.split(","))
        target = (x, y)
    else:
        target = (origin[0] + TARGET_DISTANCE, origin[1])
    aim_at(cfg, overworld, target)
    time.sleep(1.0)  # the runner's own world read follows: stay inside the burst of 3

    print(
        f"M7 smoke (A16): {cfg.name} ({cid}) on {cfg.world} "
        f"→ {args.seconds:.0f}s, goto {target} from {origin}, base {args.base_url}",
        flush=True,
    )
    metrics = M7AcceptanceMetrics(
        overworld_map_id=overworld,
        origin=origin,
        target=target,
        target_seconds=args.seconds,
    )
    metrics, elapsed = run_smoke(client, cfg, cid, metrics, timeout_s=args.timeout)
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    # A shorter practice run still fails on deaths, misses, loops and API
    # errors; navigation and regen are judged only on (nearly) the full hour.
    full_hour = args.seconds >= TARGET_SECONDS * 0.95
    failures = list(metrics.failures(full_hour=full_hour))
    if elapsed + 1.0 < args.seconds:
        failures.append(f"ran {elapsed:.0f}s < target {args.seconds:.0f}s")
    try:
        alive = client.self_(cid).get("alive", True)
        if not alive:
            failures.append("character not alive at end")
    except ApiError as e:
        failures.append(f"self read failed: {e.code}")
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M7 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
