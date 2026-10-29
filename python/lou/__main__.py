"""python -m lou create|run|status <profile>: Layered Lou, the v3 agent (docs/v3/SPEC.md).

``run`` plays until Ctrl-C, or until no milestone has come for the stall
limit, then prints the run report. Her levers come from the profile's
``[levers]`` table. What she learns is kept under ``python/.state/``:
``lou/<world>.<id>.memory.json`` (clues, entrances, shops, levels, deaths),
the world knowledge base (tiles and door warps), ``<profile>.<id>.ledger.json``
(milestones) and ``<profile>.<id>.lou.jsonl`` (the trace).
"""

from __future__ import annotations

import signal
import sys
import threading
import tomllib

from agentrealm_adk import config, supplies
from agentrealm_adk.character_select import CharacterSelectionError, create, resolve_character_id
from agentrealm_adk.cli import parser, print_self, profile_parser
from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.knowledge_base import KnowledgeBaseError
from agentrealm_adk.knowledge_base import load as load_knowledge
from agentrealm_adk.knowledge_base import save as save_knowledge
from agentrealm_adk.llm import PlannerConfigError, check_client

from adk3.core.ledger import Ledger
from adk3.core.loop import Run, Settings
from adk3.realm.body import Body
from adk3.realm.memory import Memory
from adk3.realm import value
from adk3.realm.value import Levers

from .agent import Lou
from .strategist import Strategist


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


def levers_of(cfg: config.CharacterConfig) -> Levers:
    with open(cfg.path, "rb") as f:
        return Levers.of(tomllib.load(f).get("levers"))


def run(client: Client, cfg: config.CharacterConfig, cid: int, stall_minutes: float) -> int:
    print(f"supplies reference: {supplies.load()}")
    try:
        strategist = Strategist.from_env()
        failed = check_client(strategist.client, strategist.cfg)
        if failed is not None:
            print(f"strategist: startup check failed: {failed}", file=sys.stderr)
    except PlannerConfigError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        kb = load_knowledge(cfg.world)
    except (KnowledgeBaseError, OSError) as e:
        print(f"knowledge base: {e}", file=sys.stderr)
        return 1
    state_dir = config.STATE_DIR
    memory = Memory.load(state_dir / "lou" / f"{cfg.world}.{cid}.memory.json")
    body = Body(client, cid, memory, kb)
    try:
        world = body.start()
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 2
    levers = levers_of(cfg)
    value.use_world(cfg.world)
    print(f"world {world.get('code')} at {body.tick_hz}Hz, {world.get('level_count')} levels; levers {levers}")
    lou = Lou(body, levers, strategist, state_dir / f"{cfg.profile}.{cid}.lou.jsonl")
    strategist.record = lou.record
    ledger = Ledger.load(state_dir / f"{cfg.profile}.{cid}.ledger.json")
    loop = Run(lou, ledger, Settings(stall_minutes=stall_minutes), out=print)
    stop = threading.Event()
    thread = threading.Thread(target=lambda: loop.run(stop.is_set), daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # shutting down: a second Ctrl-C must not cut the save short
        print("stopping")
        stop.set()
        thread.join(loop.settings.park_seconds + 30)  # she walks to safe ground before stopping
    finally:
        lou.close()
        save_knowledge(kb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
