#!/usr/bin/env python3
"""A40: Live M11 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M11 done-when).

Plays until the gate passes or the wall-clock limit is reached. The character
must start on the overworld (sleeping characters are woken with one ``Wait``,
downed ones are waited out). Pass criteria are in
``agentrealm_agent/m11_acceptance.py`` and the README. Requires AGENTREALM_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
PYTHON = REPO / "python"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.m11_acceptance import TARGET_SECONDS, M11AcceptanceMetrics  # noqa: E402
from smoke_olympuff_common import (  # noqa: E402
    DEFAULT_BASE,
    navigation_start,
    run_smoke,
    wake,
)

DEFAULT_PROFILE = PYTHON / "characters" / "olympuff_m11.toml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M11 acceptance smoke test on Olympuff (A40).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/olympuff_m11.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument(
        "--seconds",
        type=float,
        default=TARGET_SECONDS,
        help=f"max wall-clock seconds before stopping (default {TARGET_SECONDS:.0f})",
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
        overworld, _origin = navigation_start(client, cid)
    except (ApiError, ValueError) as e:
        print(f"start: {e}", file=sys.stderr)
        return 2
    time.sleep(1.0)

    print(
        f"M11 smoke (A40): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ up to {args.seconds:.0f}s on overworld {overworld}, base {args.base_url}",
        flush=True,
    )
    metrics = M11AcceptanceMetrics(overworld_map_id=overworld, target_seconds=args.seconds)
    metrics, elapsed = run_smoke(client, cfg, cid, metrics, timeout_s=args.timeout)
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    full_run = args.seconds >= TARGET_SECONDS * 0.95
    failures = list(metrics.failures(full_run=full_run))
    if full_run and elapsed + 1.0 < args.seconds and not metrics.milestone_ok():
        failures.append(f"ran {elapsed:.0f}s < limit {args.seconds:.0f}s without passing the gate")
    try:
        alive = client.self_(cid).get("alive", True)
        if not alive:
            failures.append("character not alive at end")
    except ApiError as e:
        failures.append(f"self read failed: {e.code}")
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M11 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
