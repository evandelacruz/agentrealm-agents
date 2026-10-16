"""python -m agentrealm_agent create|run|status|metrics|compare-metrics ..."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from agentrealm_adk import config
from agentrealm_adk.character_select import CharacterSelectionError, create, resolve_character_id
from agentrealm_adk.cli import parser, profile_parser, status
from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.knowledge_base import KnowledgeBase, KnowledgeBaseError, load as load_knowledge, save as save_knowledge
from agentrealm_adk.park import ABORT_JOIN_SECONDS, DEFAULT_PARK_SECONDS, PARK_SECONDS_HELP, install_stop_signals
from agentrealm_adk.run_metrics import RunMetrics, compare_run_metrics, load_metrics_source, metrics_from_trace
from agentrealm_adk.supplies import load_for_run as load_supplies

from .runner import Runner
from .strategist import PlannerConfigError, Strategist

# How long `run` waits for the driver thread to stop before saving, once it
# has stopped waiting on the park phase (A66): the driver returned, or a
# second signal set ``abort``.
SHUTDOWN_JOIN_SECONDS = ABORT_JOIN_SECONDS

def main(argv: list[str] | None = None) -> int:
    ap, sub = parser("agentrealm_agent", "Reference agent for Agent Realm.")

    run_p = sub.add_parser(
        "run", parents=[profile_parser()], help="drive one character until interrupted, then park it on safe ground"
    )
    run_p.add_argument("--park-seconds", type=float, default=DEFAULT_PARK_SECONDS, help=PARK_SECONDS_HELP)
    run_p.add_argument(
        "--no-planner",
        action="store_true",
        help="test mode: play without the AI planner (the built-in plan from policy.goals)",
    )
    sub.add_parser("status", parents=[profile_parser()], help="print the character's self and position")

    # Offline: needs no key, so no --character-name. The operand is a trace, or
    # a profile plus --character-id to build the keyed trace path.
    metrics_p = sub.add_parser("metrics", help="summarize run metrics from a trace")
    metrics_p.add_argument("source", help="trace (.jsonl), or behavior profile (.toml) with --character-id")
    metrics_p.add_argument(
        "--character-id",
        type=int,
        default=None,
        help="with a profile: the character whose trace to read (overrides AGENTREALM_CHARACTER_ID)",
    )

    cmp = sub.add_parser(
        "compare-metrics",
        help="numeric deltas between two runs (trace or metrics JSON paths)",
    )
    cmp.add_argument("baseline", help="baseline trace (.jsonl) or metrics snapshot (.json)")
    cmp.add_argument("candidate", help="candidate trace or snapshot")

    args = ap.parse_args(argv)

    if args.cmd == "compare-metrics":
        return compare_metrics_cmd(args.baseline, args.candidate)
    if args.cmd == "metrics":
        return metrics(args.source, character_id=args.character_id)

    try:
        cfg = config.load(args.profile)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2

    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    planner: Strategist | None = None
    if args.cmd == "run":
        try:
            planner = Strategist.off() if args.no_planner else Strategist.from_env()
            planner.check()  # a refused key stops here, before play
        except PlannerConfigError as e:
            print(e, file=sys.stderr)
            return 2
    client = Client(args.base_url, args.api_key)
    if args.cmd == "create":
        return create(client, cfg, name=args.name.strip(), avatar=args.avatar.strip(), model_agent=args.model_agent.strip())
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
    if args.cmd == "status":
        return status(client, cfg, cid)
    return run(client, cfg, cid, planner, park_seconds=args.park_seconds)


def _metrics_from_path(path: Path) -> RunMetrics:
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no such file")
    try:
        return load_metrics_source(path)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


def compare_metrics_cmd(baseline: str, candidate: str) -> int:
    try:
        base = _metrics_from_path(Path(baseline))
        cand = _metrics_from_path(Path(candidate))
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    print(json.dumps(compare_run_metrics(base, cand), sort_keys=True))
    return 0


def metrics(source: str, *, character_id: int | None) -> int:
    """Print the last run's metrics.

    A ``.toml`` source is a profile: ``--character-id`` (or
    ``AGENTREALM_CHARACTER_ID``) picks its keyed trace. Anything else is read
    as a trace, metrics snapshot or ``metrics`` capture, as ``compare-metrics`` does.
    """
    path = Path(source)
    if path.suffix.lower() != ".toml":
        if character_id is not None:
            print("--character-id goes with a profile (.toml), not a trace path", file=sys.stderr)
            return 2
        try:
            summary = _metrics_from_path(path).to_dict()
        except (OSError, ValueError) as e:
            print(e, file=sys.stderr)
            return 2
        print(f"{path.name}: {json.dumps(summary, sort_keys=True)}")
        return 0
    try:
        cfg = config.load(path)
        cid = resolve_character_id(None, cfg, character_id=character_id)
    except (OSError, ValueError) as e:  # ConfigError, CharacterSelectionError and bad TOML are ValueErrors
        print(e, file=sys.stderr)
        return 2
    trace = cfg.trace_path(cid)
    if not trace.is_file():
        print(f"{cfg.profile}: no trace at {trace}", file=sys.stderr)
        return 1
    summary = metrics_from_trace(trace).to_dict()
    print(f"{cfg.profile}: {json.dumps(summary, sort_keys=True)}")
    return 0


def run(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    planner: Strategist | None = None,
    *,
    park_seconds: float = DEFAULT_PARK_SECONDS,
) -> int:
    """Play until SIGINT (Ctrl-C) or SIGTERM, then park for up to
    ``park_seconds`` (A66); a second signal stops the park and exits
    within ``SHUTDOWN_JOIN_SECONDS``."""
    try:
        client.self_(cid)
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 2
    try:
        world_knowledge = load_knowledge(cfg.world)
    except (KnowledgeBaseError, OSError) as e:
        print(f"knowledge base: {e}", file=sys.stderr)
        return 2
    stop, abort = threading.Event(), threading.Event()

    def out(line: str) -> None:
        print(line, flush=True)

    load_supplies(world_knowledge, out)
    thread = threading.Thread(
        target=_drive,
        args=(cfg, client, cid, stop, abort, out, world_knowledge, planner, park_seconds),
        daemon=True,
    )
    restore_signals = install_stop_signals(stop, abort, out, park_seconds)
    thread.start()
    try:
        while thread.is_alive() and not abort.is_set():
            thread.join(0.5)  # the runner returns after its park phase
    finally:
        restore_signals()
        stop.set()
        thread.join(SHUTDOWN_JOIN_SECONDS)
        try:
            save_knowledge(world_knowledge)
        except OSError as e:
            out(f"knowledge base {world_knowledge.world_code}: not saved: {e}")
    return 0


def _drive(
    cfg: config.CharacterConfig,
    client: Client,
    cid: int,
    stop: threading.Event,
    abort: threading.Event,
    out,
    knowledge: KnowledgeBase,
    planner: Strategist | None,
    park_seconds: float,
) -> None:
    try:
        Runner(
            cfg, client, cid, stop, out, knowledge=knowledge, strategist=planner, park_seconds=park_seconds, abort=abort
        ).run()
    except ApiError as e:
        out(f"[{cfg.profile}] stopped: {e}")
    except Exception as e:
        out(f"[{cfg.profile}] crashed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
