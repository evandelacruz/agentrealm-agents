"""Our death chests, carried from run to run (A122).

``WorldModel.death_chests`` holds every chest our deaths dropped until one is
seen empty or gone. The server names only the latest (``death_chest`` on
``self``, B124), so an older one would be lost at exit. The runner loads
them from the world's knowledge base at start (:func:`load_death_chests`)
and writes them back at exit (:func:`save_death_chests`). Loaded, they feed
State ``death_chests`` and Recover as if dropped this run.

Stored per character, since a chest is its dropper's to come back for::

    "death_chests": {"<character id>": {"<chest id>": {"map_id", "x", "y",
                                                       "died", "contents"}}}

``contents`` is the subtype codes last seen in it, absent until it was seen
in reach.
"""

from __future__ import annotations

from typing import Any

from . import KnowledgeBase
from ..world import DeathChest, WorldModel

DEATH_CHESTS_KEY = "death_chests"


def load_death_chests(kb: KnowledgeBase | None, w: WorldModel) -> None:
    """Fill ``w.death_chests`` with this character's rows in ``kb``; a row that does not parse is skipped."""
    if kb is None:
        return
    with kb.lock:
        raw = kb.extra.get(DEATH_CHESTS_KEY)
        rows = raw.get(str(w.character_id)) if isinstance(raw, dict) else None
        rows = dict(rows) if isinstance(rows, dict) else {}
    for key, row in rows.items():
        parsed = _chest(key, row)
        if parsed is not None and parsed[0] not in w.death_chests:
            w.death_chests[parsed[0]] = parsed[1]


def save_death_chests(kb: KnowledgeBase | None, w: WorldModel) -> None:
    """Write this character's remembered death chests into ``kb``, replacing its rows; none removes them."""
    if kb is None:
        return
    rows = {str(chest_id): _row(c) for chest_id, c in sorted(w.death_chests.items())}
    with kb.lock:
        raw = kb.extra.get(DEATH_CHESTS_KEY)
        by_character: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if rows:
            by_character[str(w.character_id)] = rows
        else:
            by_character.pop(str(w.character_id), None)
        if by_character:
            kb.extra[DEATH_CHESTS_KEY] = by_character
        else:
            kb.extra.pop(DEATH_CHESTS_KEY, None)


def _row(c: DeathChest) -> dict[str, Any]:
    row: dict[str, Any] = {"map_id": c.map_id, "x": c.pos[0], "y": c.pos[1], "died": c.died}
    if c.contents is not None:
        row["contents"] = list(c.contents)
    return row


def _chest(key: str, row: Any) -> tuple[int, DeathChest] | None:
    if not isinstance(row, dict):
        return None
    try:
        chest_id = int(key)
        chest = DeathChest(int(row["map_id"]), (int(row["x"]), int(row["y"])), int(row.get("died", 0)))
    except (KeyError, TypeError, ValueError):
        return None
    contents = row.get("contents")
    if isinstance(contents, list) and all(isinstance(c, str) for c in contents):
        chest.contents = list(contents)
    return chest_id, chest
