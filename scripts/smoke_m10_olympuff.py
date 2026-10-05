#!/usr/bin/env python3
"""A33: Live M10 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M10 done-when).

Explores from wherever the character stands for the target duration (default
one hour). Pass criteria are in ``agentrealm_agent/m10_acceptance.py`` and the
README. The odd-block clause is checked offline on the ``ODD_BUSH`` fixture.
Requires AGENTREALM_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.acceptance_smoke import run_acceptance_smoke, wake  # noqa: E402
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.knowledge_base import load as load_knowledge  # noqa: E402
from agentrealm_agent.m10_acceptance import FULL_RUN_FRACTION, TARGET_SECONDS, M10AcceptanceMetrics  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "olympuff_m10.toml"
DEFAULT_BASE = "https://api.agentrealm.gg"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M10 acceptance smoke test on Olympuff (A33).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/olympuff_m10.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument(
        "--seconds",
        type=float,
        default=TARGET_SECONDS,
        help="wall-clock seconds to play before stopping (M10 smoke default: 3600)",
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

    try:
        wake(client, cid)
    except (ApiError, ValueError) as e:
        print(f"start: {e}", file=sys.stderr)
        return 2
    time.sleep(1.0)  # the runner's own world read follows: stay inside the burst of 3

    print(
        f"M10 smoke (A33): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ {args.seconds:.0f}s explore, base {args.base_url}",
        flush=True,
    )
    metrics = M10AcceptanceMetrics(target_seconds=args.seconds)

    def out(line: str) -> None:
        print(line, flush=True)

    metrics, elapsed = run_acceptance_smoke(
        client, cfg, cid, metrics, timeout_s=args.timeout, out=out
    )
    print(f"finished in {elapsed:.1f}s", flush=True)
    knowledge = load_knowledge(cfg.world)
    for line in metrics.summary_lines(knowledge):
        print(line, flush=True)
    full_run = args.seconds >= TARGET_SECONDS * FULL_RUN_FRACTION
    failures = list(metrics.failures(full_run=full_run, knowledge=knowledge))
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
    print("PASS: M10 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
