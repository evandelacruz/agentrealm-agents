"""The command line every agent shares: ``create``, the profile operand and
the flags that pick the character, and ``status``'s self line. Each agent's
``__main__`` adds its own ``run`` and ``status`` on top."""

from __future__ import annotations

import argparse
import os
import sys

from . import config
from .character_select import DEFAULT_CREATE_AVATAR, DEFAULT_CREATE_NAME
from .client import ApiError, Client

# Our characters play the scripted model agent.
DEFAULT_CREATE_MODEL = "agentrealm-reference/scripted"


def profile_parser() -> argparse.ArgumentParser:
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


def parser(prog: str, description: str) -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:
    """The server flags and the ``create`` command; the caller adds the rest
    to the returned subparsers."""
    ap = argparse.ArgumentParser(prog=prog, description=description)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg"))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)

    create_p = sub.add_parser("create", help="create a character in the profile's world and print its id")
    create_p.add_argument("profile", help="behavior profile .toml file")
    create_p.add_argument("--name", default=DEFAULT_CREATE_NAME, help="new character name")
    create_p.add_argument("--avatar", default=DEFAULT_CREATE_AVATAR, help="outfit code")
    create_p.add_argument("--model-agent", default=DEFAULT_CREATE_MODEL, help="model agent code")
    return ap, sub


def print_self(client: Client, cfg: config.CharacterConfig, cid: int) -> dict:
    """Read the character's self and print its self line. Raises ``ApiError``."""
    s = client.self_(cid)
    print(f"{cfg.profile} ({cid}): lives={s['lives']} alive={s['alive']} placed={s['placed']}")
    return s


def status(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
    """Print the self line; 1 if the server refuses."""
    try:
        print_self(client, cfg, cid)
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 1
    return 0
