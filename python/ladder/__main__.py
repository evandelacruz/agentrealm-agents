"""python -m ladder create|run|status ...: the same commands and profile files as Gary.

The parser and ``status``'s self line are the ADK's (``agentrealm_adk.cli``),
as Gary's are. ``status`` then prints her goal tree's readiness (A139,
``ladder.goals.dump``). ``run`` plays on her own loop (``ladder.play``, A141):
her reflexes, then the one planner question at the tree's goal (A140), off
with ``--no-planner`` or ``AGENTREALM_NO_PLANNER``, and the op carried out
over the ADK's executor and pathing (``ladder.act``).
"""

from __future__ import annotations

import sys
import threading

from agentrealm_adk import config
from agentrealm_adk.character_select import CharacterSelectionError, create, resolve_character_id
from agentrealm_adk.cli import parser, print_self, profile_parser
from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.knowledge_base import KnowledgeBaseError
from agentrealm_adk.knowledge_base import load as load_knowledge
from agentrealm_adk.knowledge_base.hostiles import load_hostiles
from agentrealm_adk.llm import PlannerConfigError
from agentrealm_adk.world import WorldModel

from ladder.ask import Planner
from ladder.goals import dump
from ladder.play import LadderRunner

def main(argv: list[str] | None = None) -> int:
    ap, sub = parser("ladder", "Ladder Lorraine, the goal-tree agent for Agent Realm.")
    run_p = sub.add_parser("run", parents=[profile_parser()], help="drive one character until interrupted")
    run_p.add_argument(
        "--no-planner",
        action="store_true",
        help="test mode: play without the AI planner (the goal tree's own op at every node)",
    )
    sub.add_parser("status", parents=[profile_parser()], help="print the character's self and her goal tree's readiness")
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
        return status(client, cfg, cid)
    try:
        planner = Planner.off() if args.no_planner else Planner.from_env()
        failed = planner.check()  # a refused key stops here, before play
    except PlannerConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if failed is not None:
        print(f"planner: startup check failed: {failed}", file=sys.stderr)
    return run(client, cfg, cid, planner)


def status(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
    """The self line, then the goal tree's readiness (A139), from two reads
    that change nothing (self and world) and the world's knowledge base, by
    the world model's own sync. A status never makes a round trip: that would
    take the events a live run on the character is owed. So what only a
    round trip serves (what is carried, gems, levels cleared) reads as none,
    and the turn's goal, which rests on it, is not shown."""
    try:
        s = print_self(client, cfg, cid)
        body = client.world(cid)
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 1
    try:
        kb = load_knowledge(cfg.world)
    except (KnowledgeBaseError, OSError) as e:
        print(f"{cfg.profile} ({cid}): knowledge base: {e}", file=sys.stderr)
        return 1
    w = WorldModel(character_id=cid)
    w.apply_self(s)
    if isinstance(body.get("level_count"), int):
        w.level_count = body["level_count"]
    load_hostiles(kb, w)
    print(f"health={w.health}/{w.max_health}; carried, gems and levels cleared are read only in play (none here)")
    for line in dump(w, kb, goal=False):
        print(line)
    return 0


def run(client: Client, cfg: config.CharacterConfig, cid: int, planner: Planner | None = None) -> int:
    """Play until Ctrl-C on her own loop (``ladder.play``): her reflexes, then
    the tree's goal, the one question about it, and the op carried out (A141)."""
    try:
        client.self_(cid)
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 2
    try:
        kb = load_knowledge(cfg.world)
    except (KnowledgeBaseError, OSError) as e:
        print(f"{cfg.profile} ({cid}): knowledge base: {e}", file=sys.stderr)
        return 1
    stop = threading.Event()
    runner = LadderRunner(cfg, client, cid, stop, planner or Planner.off(), knowledge=kb)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        print("stopping")
    finally:
        stop.set()
    return 1 if runner.failed else 0


if __name__ == "__main__":
    sys.exit(main())
