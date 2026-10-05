"""Interest list nominations for ``Investigate`` (A30, PLAYABLE_AGENT_PLAN Curiosity).

A30 nominates what can be done from where the agent stands: unread readable
cells in sight (``Read``) and NPCs within 25 blocks never spoken to (``Say``).
It also walks next to unlooked doors and A27 minimap entrance marks on any
map the knowledge base knows, through known door warps when needed
(``door_look``). Unknown zones are read by A7's spare-window
probes (respawn ring first, then the path). Scroll reads and subtype probes
(A56) are free ``Read {kind: supply}`` like readable cells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .config import Policy
from .curiosity_budget import curiosity_room
from .door_look import iter_unlooked_targets, look_key
from .investigation import cell_was_read, spoken_npc_ids
from .scroll_investigation import (
    SupplyReadTarget,
    code_was_probed,
    iter_supply_targets,
    scroll_subtype_codes,
    seen_supply_codes,
    supply_was_read,
)
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import Entity, Pos, WorldModel, chebyshev

SPEECH_RANGE = 25
# Reads from where the agent stands sort first (rank 0), then by this sub-rank,
# then nearest. Each kind keeps its own sub-rank so the tie-breakers after the
# distance (a cell for signs, a supply id for supplies) are never compared.
READ_SIGN, PROBE_SUPPLY, READ_SCROLL = 0, 1, 2
# A Read or Say refused this many times is dropped from the list for the run,
# so a target the server will not serve cannot hold Investigate above Explore.
MAX_REJECTIONS = 3


@dataclass(frozen=True)
class InterestItem:
    kind: Literal["read_block", "read_supply", "say", "look_door"]
    reason: str
    key: str  # Memory.investigate_rejections key
    map_id: int | None = None
    pos: Pos | None = None
    npc: Entity | None = None
    supply_id: int | None = None
    sort_key: tuple = ()
    charges_budget: bool = False


def read_key(map_id: int, pos: Pos) -> str:
    return f"read:{map_id}:{pos[0]},{pos[1]}"


def say_key(npc_id: int) -> str:
    return f"say:{npc_id}"


def read_supply_key(supply_id: int) -> str:
    return f"read_supply:{supply_id}"


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
                         sort_key=(0, READ_SIGN, chebyshev(here, p), p))
        )
    return out


def _supply_read_items(w: WorldModel, kb: KnowledgeBase | None, m: Memory, map_id: int, here: Pos) -> list[InterestItem]:
    """Probe unseen subtype codes once, then read unread scrolls (A56)."""
    scrolls = scroll_subtype_codes(kb)
    seen = seen_supply_codes(kb)
    targets = iter_supply_targets(w, in_sight)
    out: list[InterestItem] = []
    probe_pick: dict[str, SupplyReadTarget] = {}
    for t in targets:
        if supply_was_read(kb, t.supply_id):
            continue
        key = read_supply_key(t.supply_id)
        if _gave_up(m, key):
            continue
        dist = 0 if t.pos is None else chebyshev(here, t.pos)
        if t.code in scrolls:
            out.append(
                InterestItem(
                    "read_supply",
                    f"read scroll {t.code} id {t.supply_id}",
                    key,
                    map_id=map_id,
                    pos=t.pos,
                    supply_id=t.supply_id,
                    sort_key=(0, READ_SCROLL, dist, t.supply_id),
                )
            )
            continue
        if t.code not in seen or code_was_probed(kb, t.code):
            continue
        prev = probe_pick.get(t.code)
        if prev is None or _probe_prefers(t, prev):
            probe_pick[t.code] = t
    for code, t in sorted(probe_pick.items(), key=lambda kv: kv[0]):
        sid, pos = t.supply_id, t.pos
        key = read_supply_key(sid)
        if _gave_up(m, key):
            continue
        dist = 0 if pos is None else chebyshev(here, pos)
        out.append(
            InterestItem(
                "read_supply",
                f"probe supply code {code} id {sid}",
                key,
                map_id=map_id,
                pos=pos,
                supply_id=sid,
                sort_key=(0, PROBE_SUPPLY, dist, sid),
            )
        )
    return out


def _probe_prefers(candidate: SupplyReadTarget, current: SupplyReadTarget) -> bool:
    """Ground supplies beat carried when picking one probe per code."""
    if (current.pos is None) != (candidate.pos is None):
        return candidate.pos is not None
    return candidate.supply_id < current.supply_id


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


def _look_items(w: WorldModel, kb: KnowledgeBase | None, m: Memory, here_map: int, here: Pos) -> list[InterestItem]:
    # Overworld only: Level owns interior doors (A37).
    if w.map_level is not None and w.map_level > 0:
        return []
    out: list[InterestItem] = []
    for map_id, pos in iter_unlooked_targets(kb):
        lk = look_key(map_id, pos)
        if _gave_up(m, lk):
            continue
        on_map = map_id == here_map
        dist = chebyshev(here, pos) if on_map else 0
        out.append(
            InterestItem(
                "look_door",
                f"look entrance map {map_id} @{pos[0]},{pos[1]}",
                lk,
                map_id=map_id,
                pos=pos,
                sort_key=(2, 0 if on_map else 1, dist, map_id, pos),
                charges_budget=True,
            )
        )
    return out


def list_interest(w: WorldModel, kb: KnowledgeBase | None, policy: Policy, m: Memory) -> list[InterestItem]:
    """What the knowledge base has not finished investigating, best first."""
    if w.pos is None or w.map_id is None or investigate_blocked(w, policy):
        return []
    here = w.pos
    items = (
        _unread_blocks(w, kb, m, w.map_id, here)
        + _supply_read_items(w, kb, m, w.map_id, here)
        + _say_items(w, kb, m, here)
        + _look_items(w, kb, m, w.map_id, here)
    )
    items.sort(key=lambda it: it.sort_key)
    return items


def pick_interest_tick(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy: Policy,
    m: Memory,
    *,
    params: dict[str, float | int],
    tick: int | None = None,
) -> InterestItem | None:
    """Top interest item the curiosity budget still allows (A30)."""
    now = w.tick if tick is None else tick
    for item in list_interest(w, kb, policy, m):
        if not item.charges_budget or curiosity_room(params, m, now):
            return item
    return None
