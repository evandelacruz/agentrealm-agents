#!/usr/bin/env python3
"""A16: Live M7 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M7 done-when).

Plays one hour (default) from wherever the character stands on the overworld.
Before the runner starts it reads the world and the character's position, and
sends the agent to one target 150 blocks east of that start (or
``--target X,Y``): a directives goal (``pin_goto``), so the planner plans
around it and cannot drop it. The rest of the hour the planner plays. Pass criteria are in
``agentrealm_agent/m7_acceptance.py`` and the README. Requires AGENTREALM_API_KEY.

Sustained pacing aborts the run early with exit 1: more than
``OSCILLATION_ABORT_COUNT`` oscillation-guard give-ups within
``OSCILLATION_ABORT_TICKS`` stop the runner (m7_acceptance.py). Guard events
that gave nothing up (survival states pacing) do not count.

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
from agentrealm_agent.acceptance_smoke import DEFAULT_BASE, NO_PLANNER_HELP, navigation_start, pin_goto, planner_for, run_acceptance_smoke, wake  # noqa: E402
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.m7_acceptance import TARGET_DISTANCE, TARGET_SECONDS, M7AcceptanceMetrics  # noqa: E402
from agentrealm_agent.strategist import PlannerConfigError  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "olympuff_m7.toml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M7 acceptance smoke test on Olympuff (A16).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/olympuff_m7.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument("--no-planner", action="store_true", help=NO_PLANNER_HELP)
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
    if args.target:
        x, y = (int(v) for v in args.target.split(","))
        target = (x, y)
    else:
        target = (origin[0] + TARGET_DISTANCE, origin[1])
    pin_goto(cfg, overworld, target, planner_on=planner.enabled)
    time.sleep(1.0)  # the runner's own world read follows: stay inside the burst of 3

    print(
        f"M7 smoke (A16): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ {args.seconds:.0f}s, goto {target} from {origin}, base {args.base_url}",
        flush=True,
    )
    metrics = M7AcceptanceMetrics(
        overworld_map_id=overworld,
        origin=origin,
        target=target,
        target_seconds=args.seconds,
    )

    def out(line: str) -> None:
        print(line, flush=True)

    elapsed, _ = run_acceptance_smoke(client, cfg, cid, metrics, timeout_s=args.timeout, out=out, planner=planner)
    print(f"finished in {elapsed:.1f}s", flush=True)
    if metrics.oscillation_abort:
        print(f"ABORT: {metrics.oscillation_abort}; the agent paced instead of playing", file=sys.stderr)
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
