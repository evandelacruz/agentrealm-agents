"""Interest list nominations for ``Investigate`` (A30, PLAYABLE_AGENT_PLAN Curiosity).

A30 nominates what can be done from where the agent stands: unread readable
cells in sight (``Read``) and NPCs within 25 blocks never spoken to (``Say``).
Unknown zones are read by A7's spare-window probes (respawn ring first, then
the path). Scrolls, door and entrance looks are deferred (PLAN.md A30).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .config import Policy
from .investigation import cell_was_read, spoken_npc_ids
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import Entity, Pos, WorldModel, chebyshev

SPEECH_RANGE = 25
# A Read or Say refused this many times is dropped from the list for the run,
# so a target the server will not serve cannot hold Investigate above Explore.
MAX_REJECTIONS = 3


@dataclass(frozen=True)
class InterestItem:
    kind: Literal["read_block", "say"]
    reason: str
    key: str  # Memory.investigate_rejections key
    map_id: int | None = None
    pos: Pos | None = None
    npc: Entity | None = None
    sort_key: tuple = ()


def read_key(map_id: int, pos: Pos) -> str:
    return f"read:{map_id}:{pos[0]},{pos[1]}"


def say_key(npc_id: int) -> str:
    return f"say:{npc_id}"


def _brightness_at(w: WorldModel, map_id: int, pos: Pos) -> float:
    fact = w.zones.get(map_id, {}).get(pos)
    return fact.brightness if fact is not None else 1.0


def sight_range(w: WorldModel, map_id: int, at: Pos) -> int:
    """Readable sight: perception × zone brightness at the stand, capped at perception.

    GAME_NOTES Light (Guide, The world model). Carried light is not counted yet.
    """
    bright = _brightness_at(w, map_id, at)
    return max(1, min(w.perception, math.ceil(w.perception * bright)))


def in_sight(w: WorldModel, map_id: int, at: Pos, target: Pos) -> bool:
    return chebyshev(at, target) <= sight_range(w, map_id, at)


def investigate_blocked(w: WorldModel, policy: Policy) -> bool:
    """Curiosity pauses while a hostile is within hostile range (M10)."""
    if w.pos is None:
        return True
    here = w.pos
    return any(e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range for e in w.entities)


def _gave_up(m: Memory, key: str) -> bool:
    return m.investigate_rejections.get(key, 0) >= MAX_REJECTIONS


def _unread_blocks(w: WorldModel, kb: KnowledgeBase | None, m: Memory, map_id: int, here: Pos) -> list[InterestItem]:
    out: list[InterestItem] = []
    for p, flag in w.view.readable.items():
        if not flag or cell_was_read(kb, map_id, p) or not in_sight(w, map_id, here, p):
            continue
        key = read_key(map_id, p)
        if _gave_up(m, key):
            continue
        out.append(
            InterestItem("read_block", f"read sign @{p[0]},{p[1]}", key, map_id=map_id, pos=p,
                         sort_key=(0, chebyshev(here, p), p))
        )
    return out


def _say_items(w: WorldModel, kb: KnowledgeBase | None, m: Memory, here: Pos) -> list[InterestItem]:
    spoken = spoken_npc_ids(kb)
    out: list[InterestItem] = []
    for ent in w.entities:
        if ent.kind != "npc" or ent.id in spoken:
            continue
        dist = chebyshev(here, ent.pos)
        key = say_key(ent.id)
        if dist > SPEECH_RANGE or _gave_up(m, key):
            continue
        out.append(InterestItem("say", f"say to npc {ent.id}", key, npc=ent, sort_key=(1, dist, ent.id)))
    return out


def list_interest(w: WorldModel, kb: KnowledgeBase | None, policy: Policy, m: Memory) -> list[InterestItem]:
    """What the knowledge base has not finished investigating, best first."""
    if w.pos is None or w.map_id is None or investigate_blocked(w, policy):
        return []
    items = _unread_blocks(w, kb, m, w.map_id, w.pos) + _say_items(w, kb, m, w.pos)
    items.sort(key=lambda it: it.sort_key)
    return items


def pick_interest_tick(w: WorldModel, kb: KnowledgeBase | None, policy: Policy, m: Memory) -> InterestItem | None:
    """Top item. Every A30 item is done from where the agent stands, so none is
    charged to the curiosity budget; the cap arrives with A32."""
    items = list_interest(w, kb, policy, m)
    return items[0] if items else None
