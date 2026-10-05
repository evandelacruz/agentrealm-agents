#!/usr/bin/env python3
"""A29: Live M9 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M9 done-when).

Plays until every entrance mark within strength is looked and recorded and
the character is back on town, or the time limit is reached. Investigate (A30)
walks to the marks. Once the catalog is complete the script adds
``travel:town`` to the profile's directives file
(``characters/<profile>.directives.toml``, A8), so Travel (A27) walks the
agent home; the file's original contents are restored when the run ends.
Pass criteria are in ``agentrealm_agent/m9_acceptance.py`` and the README.
Requires AGENTREALM_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.acceptance import navigation_start, wake  # noqa: E402
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.directives import Directives, default_directives, load_directives  # noqa: E402
from agentrealm_agent.knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.m9_acceptance import TARGET_SECONDS, M9AcceptanceMetrics, clear_entrance_looks  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402

DEFAULT_PROFILE = PYTHON / "characters" / "olympuff_m9.toml"
DEFAULT_BASE = "https://api.agentrealm.gg"
TOWN_GOAL = "travel:town"


def directives_toml(d: Directives) -> str:
    """``d`` as a directives file. JSON strings and numbers are valid TOML values."""
    lines = ["[params]"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in d.params.items()]
    head = [
        f"never_attack = {json.dumps(d.never_attack)}",
        f"goals = {json.dumps(d.goals)}",
    ]
    if d.instructions:
        head.append(f"instructions = {json.dumps(d.instructions)}")
    return "\n".join(head + [""] + lines) + "\n"


def send_to_town(path: Path) -> None:
    """Put ``travel:town`` at the end of the directives goals (A8 reloads it).

    Earlier ``travel:*`` ops are dropped so the town op is the only one Travel
    walks; other goals, params and ``never_attack`` are kept. A file that does
    not parse is replaced by defaults plus the town op.
    """
    try:
        d = load_directives(path)
    except (OSError, tomllib.TOMLDecodeError):
        d = default_directives()
    d.goals = [g for g in d.goals if not g.strip().startswith("travel:")] + [TOWN_GOAL]
    path.write_text(directives_toml(d))


def restore_file(path: Path, original: bytes | None) -> None:
    """Put back the directives file as it was before the run (absent stays absent)."""
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(original)


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: M9AcceptanceMetrics,
    *,
    timeout_s: float,
) -> tuple[M9AcceptanceMetrics, float]:
    stop = threading.Event()
    metrics.stop = stop
    directives_path = cfg.directives_path
    original = directives_path.read_bytes() if directives_path.is_file() else None
    started = time.monotonic()
    knowledge: KnowledgeBase = load_knowledge(cfg.world)

    def out(line: str) -> None:
        print(line, flush=True)

    cleared = clear_entrance_looks(knowledge)
    if cleared:
        out(f"[{cfg.profile}] cleared {cleared} entrance look(s) from earlier runs: only this run's looks count")

    def entrances_done() -> None:
        out(f"[{cfg.profile}] entrance catalog complete: {TOWN_GOAL} via {directives_path.name}")
        send_to_town(directives_path)

    metrics.on_entrances_done = entrances_done
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
    try:
        thread.join()
    finally:
        stop.set()
        restore_file(directives_path, original)
    elapsed = time.monotonic() - started
    try:
        save_knowledge(knowledge)
    except OSError as e:
        out(f"knowledge base {knowledge.world_code}: not saved: {e}")
    return metrics, elapsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M9 acceptance smoke test on Olympuff (A29).")
    ap.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="behavior profile TOML (default: python/characters/olympuff_m9.toml)",
    )
    ap.add_argument("--character-id", type=int, default=None)
    ap.add_argument("--character-name", default=None)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument(
        "--seconds",
        type=float,
        default=TARGET_SECONDS,
        help="wall-clock seconds before stopping (default: M9 gate time budget)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=TARGET_SECONDS + 900.0,
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
        f"M9 smoke (A29): profile {cfg.profile} character {cid} on {cfg.world} "
        f"→ up to {args.seconds:.0f}s on overworld {overworld}, base {args.base_url}",
        flush=True,
    )
    metrics = M9AcceptanceMetrics(target_seconds=args.seconds)
    metrics, elapsed = run_smoke(client, cfg, cid, metrics, timeout_s=args.timeout)
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)

    full_run = args.seconds >= TARGET_SECONDS * 0.95
    failures = list(metrics.failures(full_run=full_run))
    if full_run and elapsed + 1.0 < args.seconds and not metrics.entrances_ok():
        failures.append(f"ran {elapsed:.0f}s < target {args.seconds:.0f}s without finishing entrances")
    try:
        alive = client.self_(cid).get("alive", True)
        if not alive:
            failures.append("character not alive at end")
    except ApiError as e:
        failures.append(f"self read failed: {e.code}")
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M9 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
