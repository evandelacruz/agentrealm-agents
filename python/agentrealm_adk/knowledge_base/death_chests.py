"""Our death chests, carried from run to run (A122).

``WorldModel.death_chests`` holds every chest our deaths dropped until one is
seen empty or gone. The server names only the latest (``death_chest`` on
``self``, B124), so an older one would be lost at exit. The runner loads
them from the world's knowledge base at start (:func:`load_death_chests`)
and writes them back at exit (:func:`save_death_chests`). Loaded, they feed
State ``death_chests`` and Recover as if dropped this run.

Stored per character, since a chest is its dropper's to come back for::

    "death_chests": {"<character id>": {"<chest id>": {"map_id", "x", "y",
                                                       "died", "contents", "seen"}}}

``contents`` is the subtype codes it holds, absent while unknown: expected
from what the character carried at the death (``seen`` false, A167), or last
seen in reach (``seen`` true; a row written before A167 has no ``seen`` and
was seen). The ids of chests seen empty or gone are kept too, the latest
``GONE_KEPT`` per character, so a served ``death_chest`` that still names one
never brings it back in a later run::

    "death_chests_gone": {"<character id>": [<chest id>, ...]}   # oldest first
"""

from __future__ import annotations

from typing import Any

from . import KnowledgeBase
from ..world import DeathChest, WorldModel

DEATH_CHESTS_KEY = "death_chests"
DEATH_CHESTS_GONE_KEY = "death_chests_gone"
# Gone chest ids kept per character. The server names only our latest death
# chest, so only the latest few gone ones can ever be named again.
GONE_KEPT = 8


def load_death_chests(kb: KnowledgeBase | None, w: WorldModel) -> None:
    """Fill ``w.death_chests`` with this character's rows in ``kb``; a row that does not parse is skipped."""
    if kb is None:
        return
    with kb.lock:
        rows = _of_character(kb, DEATH_CHESTS_KEY, w)
        rows = dict(rows) if isinstance(rows, dict) else {}
        gone = _of_character(kb, DEATH_CHESTS_GONE_KEY, w)
        gone = list(gone) if isinstance(gone, list) else []
    for chest_id in gone:
        if isinstance(chest_id, int) and not isinstance(chest_id, bool) and chest_id not in w.death_chests_gone:
            w.death_chests_gone.append(chest_id)
    for key, row in rows.items():
        parsed = _chest(key, row)
        if parsed is not None and parsed[0] not in w.death_chests and parsed[0] not in w.death_chests_gone:
            w.death_chests[parsed[0]] = parsed[1]


def save_death_chests(kb: KnowledgeBase | None, w: WorldModel) -> None:
    """Write this character's remembered death chests and the latest ``GONE_KEPT``
    gone ids into ``kb``, replacing its rows; none removes them."""
    if kb is None:
        return
    rows = {str(chest_id): _row(c) for chest_id, c in sorted(w.death_chests.items())}
    with kb.lock:
        _set_of_character(kb, DEATH_CHESTS_KEY, w, rows)
        _set_of_character(kb, DEATH_CHESTS_GONE_KEY, w, w.death_chests_gone[-GONE_KEPT:])


def _of_character(kb: KnowledgeBase, key: str, w: WorldModel) -> Any:
    raw = kb.extra.get(key)
    return raw.get(str(w.character_id)) if isinstance(raw, dict) else None


def _set_of_character(kb: KnowledgeBase, key: str, w: WorldModel, value: Any) -> None:
    """This character's ``value`` under ``key``; an empty one removes it, and the key once no character has one."""
    raw = kb.extra.get(key)
    by_character: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    if value:
        by_character[str(w.character_id)] = value
    else:
        by_character.pop(str(w.character_id), None)
    if by_character:
        kb.extra[key] = by_character
    else:
        kb.extra.pop(key, None)


def _row(c: DeathChest) -> dict[str, Any]:
    row: dict[str, Any] = {"map_id": c.map_id, "x": c.pos[0], "y": c.pos[1], "died": c.died}
    if c.contents is not None:
        row["contents"], row["seen"] = list(c.contents), c.seen
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
        chest.contents, chest.seen = list(contents), row.get("seen", True) is not False
    return chest_id, chest
