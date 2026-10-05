"""Pick the character to play, or create one, for a behavior profile (A59).

A profile never names a character. The character comes from the command line
or the environment, and only an explicit ``create`` makes a new one.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .client import ApiError

if TYPE_CHECKING:
    from .client import Client
    from .config import CharacterConfig

# Defaults for `create` when --name / --avatar are not passed.
DEFAULT_CREATE_NAME = "Agent"
DEFAULT_CREATE_AVATAR = "default"


class CharacterSelectionError(ValueError):
    """The user did not pick exactly one character to play."""


def find_character_ids_by_name(client: Client, world: str, name: str) -> list[tuple[int, str]]:
    """``list_characters`` rows matching ``name`` in ``world``, as ``(id, name)``."""
    out: list[tuple[int, str]] = []
    for row in client.list_characters():
        if row.get("name") == name and row.get("world_code") == world:
            out.append((int(row["id"]), str(row.get("name") or name)))
    return out


def resolve_character_id(
    client: Client | None,
    cfg: CharacterConfig,
    *,
    character_id: int | None = None,
    character_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """One character id to play.

    An explicit flag wins over the environment: ``--character-id``, else
    ``--character-name`` (looked up in the profile's world), else
    ``AGENTREALM_CHARACTER_ID``. Passing both flags is an error.
    """
    name = (character_name or "").strip() or None
    if character_id is not None and name:
        raise CharacterSelectionError("pass --character-id or --character-name, not both")
    if character_id is not None:
        return character_id
    if name:
        return _id_for_name(client, cfg, name)
    env = environ if environ is not None else os.environ
    raw = env.get("AGENTREALM_CHARACTER_ID", "").strip()
    if not raw:
        raise CharacterSelectionError(
            "pass --character-id, --character-name, or set AGENTREALM_CHARACTER_ID"
        )
    try:
        return int(raw)
    except ValueError as e:
        raise CharacterSelectionError("AGENTREALM_CHARACTER_ID must be an integer") from e


def _id_for_name(client: Client | None, cfg: CharacterConfig, name: str) -> int:
    """The one character called ``name`` in the profile's world."""
    if client is None:
        raise CharacterSelectionError("--character-name needs an API key")
    matches = find_character_ids_by_name(client, cfg.world, name)
    if not matches:
        raise CharacterSelectionError(
            f"no character named {name!r} in world {cfg.world!r}; create one or pick another name"
        )
    if len(matches) > 1:
        choices = ", ".join(f"{n} (id {i})" for i, n in matches)
        raise CharacterSelectionError(
            f"several characters named {name!r} in world {cfg.world!r}: {choices}"
        )
    return matches[0][0]


def create(client: Client, cfg: CharacterConfig, *, name: str, avatar: str, model_agent: str) -> int:
    """Create a character in the profile's world and print its id; writes no local file."""
    try:
        s = client.create_character(cfg.world, name, avatar, model_agent)
    except ApiError as e:
        print(f"{cfg.profile}: {e}", file=sys.stderr)
        return 1
    print(s["id"])
    return 0
