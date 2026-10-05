"""python -m agentrealm_agent create|run|status|metrics|compare-metrics ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import config
from .character_select import (
    DEFAULT_CREATE_AVATAR,
    DEFAULT_CREATE_NAME,
    CharacterSelectionError,
    create,
    resolve_character_id,
)
from .client import ApiError, Client
from .knowledge_base import KnowledgeBase, KnowledgeBaseError, load as load_knowledge, save as save_knowledge
from .run_metrics import RunMetrics, compare_run_metrics, load_metrics_source, metrics_from_trace
from .runner import Runner

# How long `run` waits for the driver thread to stop before saving.
SHUTDOWN_JOIN_SECONDS = 5.0

# The reference runner's characters play the scripted model agent.
DEFAULT_CREATE_MODEL = "agentrealm-reference/scripted"


def _profile_parser() -> argparse.ArgumentParser:
    """The profile operand and the flags that pick the character to play."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("profile", help="behavior profile .toml file")
    p.add_argument(
        "--character-id",
        type=int,
        default=None,
        help="character id to play (overrides AGENTREALM_CHARACTER_ID)",
    )
    p.add_argument(
        "--character-name",
        default=None,
        help="pick an existing character by name in the profile's world (overrides AGENTREALM_CHARACTER_ID)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentrealm_agent", description="Reference agent for Agent Realm.")
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg"))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)

    create_p = sub.add_parser("create", help="create a character in the profile's world and print its id")
    create_p.add_argument("profile", help="behavior profile .toml file")
    create_p.add_argument("--name", default=DEFAULT_CREATE_NAME, help="new character name")
    create_p.add_argument("--avatar", default=DEFAULT_CREATE_AVATAR, help="outfit code")
    create_p.add_argument("--model-agent", default=DEFAULT_CREATE_MODEL, help="model agent code")

    sub.add_parser("run", parents=[_profile_parser()], help="drive one character until interrupted")
    sub.add_parser("status", parents=[_profile_parser()], help="print the character's self and position")

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
    return {"run": run, "status": status}[args.cmd](client, cfg, cid)


def status(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
    try:
        s = client.self_(cid)
        print(f"{cfg.profile} ({cid}): lives={s['lives']} alive={s['alive']} placed={s['placed']}")
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 1
    return 0


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
    """Print the last run's metrics from a trace, or from a profile's keyed trace."""
    path = Path(source)
    if path.suffix == ".jsonl":
        if character_id is not None:
            print("--character-id goes with a profile, not a trace path", file=sys.stderr)
            return 2
        label = path.name
    else:
        try:
            cfg = config.load(path)
            cid = resolve_character_id(None, cfg, character_id=character_id)
        except (config.ConfigError, OSError, CharacterSelectionError) as e:
            print(e, file=sys.stderr)
            return 2
        path = cfg.trace_path(cid)
        label = cfg.profile
    if not path.is_file():
        print(f"{label}: no trace at {path}", file=sys.stderr)
        return 1
    summary = metrics_from_trace(path).to_dict()
    print(f"{label}: {json.dumps(summary, sort_keys=True)}")
    return 0


def run(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
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
    stop = threading.Event()

    def out(line: str) -> None:
        print(line, flush=True)

    thread = threading.Thread(
        target=_drive,
        args=(cfg, client, cid, stop, out, world_knowledge),
        daemon=True,
    )
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        out("stopping; the character stays in the world where it stands")
    finally:
        stop.set()
        deadline = time.monotonic() + SHUTDOWN_JOIN_SECONDS
        thread.join(max(0.0, deadline - time.monotonic()))
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
    out,
    knowledge: KnowledgeBase,
) -> None:
    try:
        Runner(cfg, client, cid, stop, out, knowledge=knowledge).run()
    except ApiError as e:
        out(f"[{cfg.profile}] stopped: {e}")
    except Exception as e:
        out(f"[{cfg.profile}] crashed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
