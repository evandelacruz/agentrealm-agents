"""Hostile NPC types and where hostiles keep, carried from run to run (free-play run 6).

``WorldModel.hostile_types`` and ``WorldModel.sightings`` are learned while
the agent plays. Kept only in memory, they were lost at exit: run 6 started
next to the NPC that killed run 5 and did not know its type was hostile. So
the runner loads them from the world's knowledge base at start
(:func:`load_hostiles`) and writes them back at exit (:func:`save_hostiles`).
Loaded, they feed the same tests as anything learned this run: Flee and
Retreat (``survival.is_hostile``), and the ground Gather and Detour keep off
(``hostile_ground.known_reach``, ``gather_safe.route_clear``).

Stored in the knowledge base as::

    "npc_types": {"<npc type code>": {"hostile": true}, ...}
    "hostile_sightings": {"<npc id>": {"code", "map_id", "x", "y", "tick",
                                       "home": [x, y], "post", "reach"}, ...}

A type is saved once it has shown it is hostile (``survival.known_hostile``).
A sighting is saved for every NPC of such a type: its post and reach, or the
cell it was last seen on. A loaded sighting is forgotten by the same rules as
one seen this run (``WorldModel._note_sightings``): its cell in sight with
the NPC gone, an ``NPCDied``, or, keeping no post, ``SIGHTING_TICKS`` of
world time unseen. Forgotten, it is removed from the file at exit.
"""

from __future__ import annotations

from typing import Any

from .knowledge_base import KnowledgeBase
from .survival import known_hostile
from .world import Entity, Sighting, WorldModel

SIGHTINGS_KEY = "hostile_sightings"


def load_hostiles(kb: KnowledgeBase | None, w: WorldModel) -> set[int]:
    """Fill ``w.hostile_types`` and ``w.sightings`` from ``kb``; the NPC ids loaded.

    Rows that do not parse are skipped. A sighting already in ``w`` is kept.
    """
    if kb is None:
        return set()
    loaded: set[int] = set()
    with kb.lock:
        for code, row in kb.npc_types.items():
            if isinstance(row, dict) and row.get("hostile") is True:
                w.hostile_types.add(("npc", code))
        raw = kb.extra.get(SIGHTINGS_KEY)
        rows = dict(raw) if isinstance(raw, dict) else {}
    for key, row in rows.items():
        s = _sighting(key, row)
        if s is None or ("npc", s.entity.id) in w.sightings:
            continue
        w.sightings[("npc", s.entity.id)] = s
        loaded.add(s.entity.id)
    return loaded


def save_hostiles(kb: KnowledgeBase | None, w: WorldModel, loaded: set[int] | None = None) -> None:
    """Write this run's hostile NPC types and their sightings into ``kb``.

    A row this run loaded (``loaded``, from :func:`load_hostiles`) and since
    forgot is removed; rows another character wrote are left alone.
    """
    if kb is None:
        return
    hostile = {code for kind, code in w.hostile_types if kind == "npc"}
    hostile |= {code for kind, code in w.threat.by_type if kind == "npc"}
    npcs = [s for (kind, _), s in w.sightings.items() if kind == "npc" and s.entity.code]
    hostile |= {s.entity.code for s in npcs if known_hostile(w, s.entity)}
    with kb.lock:
        for code in hostile:
            kb.npc_types.setdefault(code, {})["hostile"] = True
        raw = kb.extra.get(SIGHTINGS_KEY)
        rows: dict[str, Any] = raw if isinstance(raw, dict) else {}
        for npc_id in loaded or ():
            rows.pop(str(npc_id), None)
        for s in npcs:
            if s.entity.code in hostile:
                rows[str(s.entity.id)] = _row(s)
        if rows:
            kb.extra[SIGHTINGS_KEY] = rows
        else:
            kb.extra.pop(SIGHTINGS_KEY, None)


def _row(s: Sighting) -> dict[str, Any]:
    e = s.entity
    return {
        "code": e.code,
        "map_id": s.map_id,
        "x": e.pos[0],
        "y": e.pos[1],
        "tick": s.tick,
        "home": [s.home[0], s.home[1]],
        "post": s.post,
        "reach": s.reach,
    }


def _sighting(key: str, row: Any) -> Sighting | None:
    if not isinstance(row, dict):
        return None
    try:
        npc_id = int(key)
        pos = (int(row["x"]), int(row["y"]))
        home = (int(row["home"][0]), int(row["home"][1]))
        map_id = int(row["map_id"])
        tick = int(row["tick"])
        reach = int(row.get("reach", 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    code = row.get("code")
    if not isinstance(code, str) or not code:
        return None
    return Sighting(Entity("npc", npc_id, pos, code), map_id, tick, home, row.get("post") is True, reach)
