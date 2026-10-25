"""python -m lou create|run|status <profile>: Layered Lou, the v3 agent (docs/v3/SPEC.md).

``run`` plays until Ctrl-C, or until no milestone has come for the stall
limit, then prints the run report. The trace is
``.state/<profile>.<id>.lou.jsonl``; the milestone ledger, kept across runs,
``.state/<profile>.<id>.ledger.json``.
"""

from __future__ import annotations

import json
import sys
import threading
import time

from agentrealm_adk import config, supplies
from agentrealm_adk.character_select import CharacterSelectionError, create, resolve_character_id
from agentrealm_adk.cli import parser, print_self, profile_parser
from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.llm import PlannerConfigError, check_client

from adk3.core.controller import Controller
from adk3.core.ledger import Ledger
from adk3.core.loop import Agent, Run, Settings
from adk3.realm.body import RealmBody

from . import considerations, tasks
from .strategist import LouStrategist


def main(argv: list[str] | None = None) -> int:
    ap, sub = parser("lou", "Layered Lou, the v3 agent for Agent Realm.")
    run_p = sub.add_parser("run", parents=[profile_parser()], help="play until Ctrl-C or a stall")
    run_p.add_argument("--stall-minutes", type=float, default=Settings.stall_minutes,
                       help="stop after this long with no milestone")
    sub.add_parser("status", parents=[profile_parser()], help="print the character's self line")
    args = ap.parse_args(argv)
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
        cid = resolve_character_id(client, cfg, character_id=args.character_id, character_name=args.character_name)
    except CharacterSelectionError as e:
        print(e, file=sys.stderr)
        return 2
    if args.cmd == "status":
        try:
            print_self(client, cfg, cid)
        except ApiError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    return run(client, cfg, cid, args.stall_minutes)


def run(client: Client, cfg: config.CharacterConfig, cid: int, stall_minutes: float) -> int:
    print(f"supplies reference: {supplies.load()}")
    body = RealmBody(client, cid)
    trace_path = config.STATE_DIR / f"{cfg.profile}.{cid}.lou.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    side = open(trace_path, "a", buffering=1)

    def note(record: dict) -> None:
        side.write(json.dumps({"t": time.time(), **record}) + "\n")

    try:
        strategist = LouStrategist.from_env(body, note)
        failed = check_client(strategist.client, strategist.cfg)
        if failed is not None:
            print(f"strategist: startup check failed: {failed}", file=sys.stderr)
    except PlannerConfigError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        world = body.start()
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 2
    print(f"world {world.get('code')} at {body.tick_hz}Hz, {world.get('level_count')} levels")
    agent = Agent(strategist, tasks.build(body), Controller(considerations.ALL, considerations.WEIGHTS))
    ledger = Ledger.load(config.STATE_DIR / f"{cfg.profile}.{cid}.ledger.json")
    loop = Run(body, agent, ledger, trace_path, Settings(stall_minutes=stall_minutes))
    stop = threading.Event()
    thread = threading.Thread(target=lambda: loop.run(stop.is_set), daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("stopping")
        stop.set()
        thread.join(5)
    side.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
