#!/usr/bin/env python3
"""A60: Regen probe. Get hurt, then let Heal measure safe-zone regen.

Runs the normal agent on ``characters/regen_probe.toml`` (``on_hostile =
"fight"``) from wherever the character stands until ``regen_known`` answers,
the cap passes (default 30 minutes), or the character dies (a failure). A
"yes" is saved to the world knowledge base, where the M7 gate (A16) reads it.
Prints the verdict and exits 0 only when regen answered. Never creates a
character. Requires AGENTREALM_API_KEY.

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
from agentrealm_agent.acceptance_smoke import DEFAULT_BASE, NO_PLANNER_HELP, STOP_ON_DEATH_HELP, add_park_argument, planner_for, run_acceptance_smoke, wake  # noqa: E402
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.regen_probe import PROBE_SECONDS, RegenProbeMetrics  # noqa: E402
from agentrealm_agent.strategist import PlannerConfigError  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "regen_probe.toml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regen probe: get hurt, then measure safe-zone regen (A60).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/regen_probe.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument("--no-planner", action="store_true", help=NO_PLANNER_HELP)
    ap.add_argument("--stop-on-death", action="store_true", help=STOP_ON_DEATH_HELP)
    add_park_argument(ap)
    ap.add_argument(
        "--seconds",
        type=float,
        default=PROBE_SECONDS,
        help="cap: wall-clock seconds before giving up without a verdict (default 1800)",
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
    time.sleep(1.0)  # the runner's own reads follow: stay inside the burst of 3

    print(
        f"regen probe (A60): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ cap {args.seconds:.0f}s, base {args.base_url}",
        flush=True,
    )
    metrics = RegenProbeMetrics(stop_on_death=args.stop_on_death, target_seconds=args.seconds)

    def out(line: str) -> None:
        print(line, flush=True)

    elapsed, _ = run_acceptance_smoke(client, cfg, cid, metrics, timeout_s=args.seconds + 600.0, out=out, planner=planner, park_seconds=args.park_seconds)
    print(f"finished in {elapsed:.1f}s", flush=True)
    print(metrics.park_summary_line(), flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    failures = metrics.failures()
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print(f"PASS: safe-zone regen is {metrics.regen!r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
