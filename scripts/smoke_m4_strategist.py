#!/usr/bin/env python3
"""A36: Live M4 acceptance with the strategist (docs/PLAYABLE_AGENT_PLAN.md M4 done-when).

Plays on sandbox with the strategist enabled. Pass criteria are in
``agentrealm_agent/m4_acceptance.py`` and the README. Requires
``AGENTREALM_API_KEY``, ``AGENTREALM_STRATEGIST_MODEL``, and
``AGENTREALM_STRATEGIST_API_KEY`` or ``OPENAI_API_KEY``. The character is
chosen at run time (``--character-id`` or ``--character-name``); nothing is
created.
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
from agentrealm_agent.m4_acceptance import TARGET_SECONDS, M4AcceptanceMetrics  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402
from agentrealm_agent.strategist import StrategistConfig  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "strategist_gate.toml"
DEFAULT_BASE = "https://api.agentrealm.gg"


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: M4AcceptanceMetrics,
    *,
    timeout_s: float,
) -> tuple[M4AcceptanceMetrics, float]:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M4 strategist acceptance smoke (A36).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/strategist_gate.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument(
        "--seconds",
        type=float,
        default=TARGET_SECONDS,
        help="wall-clock seconds to play before stopping (default: 3600)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=TARGET_SECONDS + 600.0,
        help="hard timeout seconds (0 = no limit)",
    )
    args = ap.parse_args(argv)

    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    strat = StrategistConfig.from_env()
    if not strat.enabled:
        print(
            "set AGENTREALM_STRATEGIST_MODEL and AGENTREALM_STRATEGIST_API_KEY or OPENAI_API_KEY",
            file=sys.stderr,
        )
        return 2
    try:
        cfg = config.load(args.profile)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if cfg.world != "sandbox":
        print(f"expected world sandbox, got {cfg.world!r}", file=sys.stderr)
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

    metrics = M4AcceptanceMetrics(target_seconds=args.seconds)
    print(
        f"M4 smoke (A36): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ {args.seconds:.0f}s, strategist model {strat.model!r}, base {args.base_url}",
        flush=True,
    )
    metrics, elapsed = run_smoke(client, cfg, cid, metrics, timeout_s=args.timeout)
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    full = args.seconds >= TARGET_SECONDS * 0.95
    failures = list(metrics.failures(full=full))
    if elapsed + 1.0 < args.seconds:
        failures.append(f"ran {elapsed:.0f}s < target {args.seconds:.0f}s")
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M4 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
