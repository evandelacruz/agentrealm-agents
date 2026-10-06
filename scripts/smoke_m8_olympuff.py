#!/usr/bin/env python3
"""A25: Live M8 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M8 done-when).

Plays from wherever the character stands on the overworld (default one hour).
Pass criteria are in ``agentrealm_agent/m8_acceptance.py`` and the README.
Requires AGENTREALM_API_KEY. The character is chosen at run time (A59); the
script never creates one.

Plays with the AI planner (A35): set a planner key (README, The AI planner),
or pass --no-planner for the test mode.
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
from agentrealm_agent.acceptance_smoke import DEFAULT_BASE, NO_PLANNER_HELP, STOP_ON_DEATH_HELP, navigation_start, planner_for, run_acceptance_smoke, wake  # noqa: E402
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.m8_acceptance import FULL_RUN_FRACTION, TARGET_SECONDS, M8AcceptanceMetrics  # noqa: E402
from agentrealm_agent.strategist import PlannerConfigError  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "olympuff_m8.toml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M8 acceptance smoke test on Olympuff (A25).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/olympuff_m8.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument("--no-planner", action="store_true", help=NO_PLANNER_HELP)
    ap.add_argument("--stop-on-death", action="store_true", help=STOP_ON_DEATH_HELP)
    ap.add_argument(
        "--seconds",
        type=float,
        default=TARGET_SECONDS,
        help="wall-clock seconds to play before stopping (M8 default: 3600)",
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

    try:
        wake(client, cid)
        overworld, origin = navigation_start(client, cid)
    except (ApiError, ValueError) as e:
        print(f"start: {e}", file=sys.stderr)
        return 2
    time.sleep(1.0)  # the runner's own world read follows: stay inside the burst of 3

    print(
        f"M8 smoke (A25): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ {args.seconds:.0f}s on overworld map {overworld} from {origin}, base {args.base_url}",
        flush=True,
    )
    metrics = M8AcceptanceMetrics(stop_on_death=args.stop_on_death, target_seconds=args.seconds)

    def out(line: str) -> None:
        print(line, flush=True)

    elapsed, _ = run_acceptance_smoke(client, cfg, cid, metrics, timeout_s=args.timeout, out=out, planner=planner)
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    full_run = args.seconds >= TARGET_SECONDS * FULL_RUN_FRACTION
    failures = list(metrics.failures(full_run=full_run))
    if elapsed + 1.0 < args.seconds:
        failures.append(f"ran {elapsed:.0f}s < target {args.seconds:.0f}s")
    try:
        alive = client.self_(cid).get("alive", True)
        if not alive and args.stop_on_death:
            failures.append("character not alive at end")
        elif not alive:
            print("character not alive at end (respawning)", flush=True)
    except ApiError as e:
        failures.append(f"self read failed: {e.code}")
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M8 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
