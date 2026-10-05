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
from .character_select import CharacterSelectionError, resolve_character_id
from .client import ApiError, Client
from .knowledge_base import KnowledgeBase, KnowledgeBaseError, load as load_knowledge, save as save_knowledge
from .run_metrics import RunMetrics, compare_run_metrics, load_metrics_source, metrics_from_trace
from .runner import Runner

# How long `run` waits for the driver thread to stop before saving.
SHUTDOWN_JOIN_SECONDS = 5.0

DEFAULT_CREATE_NAME = "Agent"
DEFAULT_CREATE_AVATAR = "default"
DEFAULT_CREATE_MODEL = "agentrealm-reference/scripted"


def _add_character_id_flags(p: argparse.ArgumentParser, *, metrics: bool = False) -> None:
    p.add_argument(
        "--character-id",
        type=int,
        default=None,
        help="character id to play (default: AGENTREALM_CHARACTER_ID)",
    )
    if not metrics:
        p.add_argument(
            "--character-name",
            default=None,
            help="pick an existing character by name in the profile's world",
        )


def _profile_parser(name: str, help_: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("profile", help="behavior profile .toml file")
    _add_character_id_flags(p, metrics=(name == "metrics"))
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

    for name, help_ in (
        ("run", "drive one character until interrupted"),
        ("status", "print the character's self and position"),
        ("metrics", "summarize run metrics from the character's trace"),
    ):
        sub.add_parser(name, parents=[_profile_parser(name, help_)], help=help_)

    cmp = sub.add_parser(
        "compare-metrics",
        help="numeric deltas between two runs (trace or metrics JSON paths)",
    )
    cmp.add_argument("baseline", help="baseline trace (.jsonl) or metrics snapshot (.json)")
    cmp.add_argument("candidate", help="candidate trace or snapshot")

    args = ap.parse_args(argv)

    if args.cmd == "compare-metrics":
        return compare_metrics_cmd(args.baseline, args.candidate)

    try:
        cfg = config.load(args.profile)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2

    if args.cmd == "metrics":
        return metrics(cfg, character_id=args.character_id)
    if args.cmd == "create":
        if not args.api_key:
            print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
            return 2
        client = Client(args.base_url, args.api_key)
        return create(client, cfg, name=args.name.strip(), avatar=args.avatar.strip(), model_agent=args.model_agent.strip())
    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    client = Client(args.base_url, args.api_key)
    try:
        cid = resolve_character_id(
            client,
            cfg,
            character_id=args.character_id,
            character_name=getattr(args, "character_name", None),
        )
    except CharacterSelectionError as e:
        print(e, file=sys.stderr)
        return 2
    return {"run": run, "status": status}[args.cmd](client, cfg, cid)


def create(client: Client, cfg: config.CharacterConfig, *, name: str, avatar: str, model_agent: str) -> int:
    try:
        s = client.create_character(cfg.world, name, avatar, model_agent)
    except ApiError as e:
        print(f"{cfg.profile}: {e}", file=sys.stderr)
        return 1
    print(s["id"])
    return 0


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


def metrics(cfg: config.CharacterConfig, *, character_id: int | None) -> int:
    try:
        cid = resolve_character_id(None, cfg, character_id=character_id, character_name=None)
    except CharacterSelectionError as e:
        print(e, file=sys.stderr)
        return 2
    path = cfg.trace_path(cid)
    if not path.is_file():
        print(f"{cfg.profile}: no trace at {path}", file=sys.stderr)
        return 1
    summary = metrics_from_trace(path).to_dict()
    print(f"{cfg.profile}: {json.dumps(summary, sort_keys=True)}")
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
