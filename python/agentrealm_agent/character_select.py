"""Resolve which character id to play for a behavior profile (A59)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import Client
    from .config import CharacterConfig


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
    environ: os._Environ | None = None,
) -> int:
    """One character id from flags, ``AGENTREALM_CHARACTER_ID``, or ``--character-name``."""
    env = environ if environ is not None else os.environ
    cid = character_id
    if cid is None:
        raw = env.get("AGENTREALM_CHARACTER_ID", "").strip()
        if raw:
            try:
                cid = int(raw)
            except ValueError as e:
                raise CharacterSelectionError("AGENTREALM_CHARACTER_ID must be an integer") from e
    name = (character_name or "").strip() or None
    if cid is not None and name:
        raise CharacterSelectionError("pass --character-id or --character-name, not both")
    if name:
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
    if cid is None:
        raise CharacterSelectionError(
            "pass --character-id, --character-name, or set AGENTREALM_CHARACTER_ID"
        )
    return cid
