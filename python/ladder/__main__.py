"""python -m ladder create|run|status ...: the same commands and profile files as Gary.

The parser and ``status``'s self line mirror Gary's ``agentrealm_agent.__main__``
until A149 moves them into the shared ADK; his module loads his runner and
planner, so she cannot import it. ``status`` then prints her goal tree's
readiness (A139, ``ladder.goals.dump``).
"""

from __future__ import annotations

import argparse
import os
import sys

from agentrealm_agent import config
from agentrealm_agent.character_select import (
    DEFAULT_CREATE_AVATAR,
    DEFAULT_CREATE_NAME,
    CharacterSelectionError,
    create,
    resolve_character_id,
)
from agentrealm_agent.client import ApiError, Client
from agentrealm_agent.knowledge_base import KnowledgeBaseError
from agentrealm_agent.knowledge_base import load as load_knowledge
from agentrealm_agent.knowledge_base.hostiles import load_hostiles
from agentrealm_agent.world import WorldModel

from ladder.goals import dump

# Temporary, and the one tie to Stan: until her goal tree drives play (A140–A141)
# `run` is Stan's loop (sync, flee, explore). Run from `python/`, where
# `starter_agent.py` is importable.
import starter_agent

# Her characters play the scripted model agent, as Gary's do.
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
    ap = argparse.ArgumentParser(prog="ladder", description="Ladder Lorraine, the goal-tree agent for Agent Realm.")
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg"))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)

    create_p = sub.add_parser("create", help="create a character in the profile's world and print its id")
    create_p.add_argument("profile", help="behavior profile .toml file")
    create_p.add_argument("--name", default=DEFAULT_CREATE_NAME, help="new character name")
    create_p.add_argument("--avatar", default=DEFAULT_CREATE_AVATAR, help="outfit code")
    create_p.add_argument("--model-agent", default=DEFAULT_CREATE_MODEL, help="model agent code")
    sub.add_parser("run", parents=[_profile_parser()], help="drive one character until interrupted")
    sub.add_parser("status", parents=[_profile_parser()], help="print the character's self and her goal tree's readiness")
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
    return run(client, cfg, cid)


def status(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
    """The self line, then the goal tree's readiness (A139), from two reads
    that change nothing (self and world) and the world's knowledge base, by
    the world model's own sync. A status never makes a round trip: that would
    take the events a live run on the character is owed. So what only a
    round trip serves (what is carried, gems, levels cleared) reads as none,
    and the turn's goal, which rests on it, is not shown."""
    try:
        s = client.self_(cid)
        print(f"{cfg.profile} ({cid}): lives={s['lives']} alive={s['alive']} placed={s['placed']}")
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


def run(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
    """Play until Ctrl-C: Stan's loop, until her goal tree replaces it."""
    return starter_agent.run(client, cfg, cid)


if __name__ == "__main__":
    sys.exit(main())
