#!/usr/bin/env python3
"""A4: Live M6 acceptance on olympuff (docs/PLAYABLE_AGENT_PLAN.md M6 done-when).

Walks until 200 Steps apply with no movement_cooldown rejections and
POST tick goes out in under a quarter of calm windows. Requires network access,
AGENTREALM_API_KEY, and AGENTREALM_BASE_URL (default https://api.agentrealm.gg).
Pass --character-id or --character-name to pick the character to play (A59).

Plays with the AI planner (A35): set a planner key (README, The AI planner),
or pass --no-planner for the test mode.
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
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import Client  # noqa: E402
from agentrealm_agent.knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.m6_acceptance import M6AcceptanceMetrics, TARGET_STEPS  # noqa: E402
from agentrealm_agent.acceptance_smoke import NO_PLANNER_HELP, planner_for  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402
from agentrealm_agent.strategist import PlannerConfigError, Strategist  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "olympuff_walker.toml"
DEFAULT_BASE = "https://api.agentrealm.gg"


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    *,
    target_steps: int,
    timeout_s: float,
    planner: Strategist | None = None,
) -> M6AcceptanceMetrics:
    stop = threading.Event()
    metrics = M6AcceptanceMetrics(target_steps=target_steps, stop=stop)
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
        strategist=planner,
    )

    def watchdog() -> None:
        if timeout_s <= 0:
            return
        if stop.wait(timeout_s):
            return
        out(f"[{cfg.profile}] timeout after {timeout_s:.0f}s with {metrics.steps_applied} steps")
        stop.set()

    thread = threading.Thread(target=runner.run, daemon=True)
    wd = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    wd.start()
    thread.join()
    stop.set()
    try:
        save_knowledge(knowledge)
    except OSError as e:
        out(f"knowledge base {knowledge.world_code}: not saved: {e}")
    return metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M6 acceptance smoke test on olympuff (A4).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/olympuff_walker.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument("--no-planner", action="store_true", help=NO_PLANNER_HELP)
    ap.add_argument("--steps", type=int, default=TARGET_STEPS, help="applied Step count to reach")
    ap.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="wall-clock seconds before giving up (0 = no limit)",
    )
    args = ap.parse_args(argv)

    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    try:
        planner = planner_for(args.no_planner)
    except PlannerConfigError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        cfg = config.load(args.profile)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if cfg.world != "olympuff":
        print(f"expected world olympuff, got {cfg.world!r}", file=sys.stderr)
        return 2

    client = Client(args.base_url, args.api_key)
    try:
        cid = resolve_character_id(
            client,
            cfg,
            character_id=args.character_id,
            character_name=args.character_name,
        )
    except CharacterSelectionError as e:
        print(e, file=sys.stderr)
        return 2

    print(
        f"M6 smoke (A4): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ {args.steps} steps, base {args.base_url}",
        flush=True,
    )
    started = time.monotonic()
    metrics = run_smoke(client, cfg, cid, target_steps=args.steps, timeout_s=args.timeout, planner=planner)
    elapsed = time.monotonic() - started
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    failures = metrics.failures()
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M6 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
