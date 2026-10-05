"""Odd-block detector: nominate breakables that look out of place (A31).

PLAYABLE_AGENT_PLAN Curiosity **Odd-block detector**: one rock in a garden, one
bush in a wheat field. A block is odd when its type is rare in a 7×7
neighbourhood (one or two of it), most surrounding cells share one other type,
and it is breakable in principle. Clue text that names the block type adds a
boost (full clue rules land in A32).
"""

from __future__ import annotations

from dataclasses import dataclass

from .break_memory import (
    BREAKABLE,
    WEAPONS,
    BreakChoice,
    break_step_cost,
    held_capabilities,
    pick_supply_for_capability,
    untried_capabilities,
)
from .curiosity_budget import curiosity_room
from .interest_list import investigate_blocked, sight_range
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import DOORS, Pos, WorldModel, chebyshev

RADIUS = 3  # 7×7 neighbourhood
RARE_MAX = 2  # at most this many cells of the same type (including self)
DOMINANT_FRAC = 0.55  # most neighbours share one other type
# Consumable tools are tried only when a clue names the block or the score is high.
TOOL_MIN_SCORE = 2.0


@dataclass(frozen=True)
class OddNomination:
    pos: Pos
    score: float
    clue_boost: float


def _map_tiles(w: WorldModel, kb: KnowledgeBase | None, map_id: int) -> dict[Pos, str]:
    view = w.maps.get(map_id)
    tiles = dict(view.tiles if view is not None else w.view.tiles)
    if kb is not None:
        with kb.lock:
            raw = (kb.maps.get(str(map_id)) or {}).get("terrain") or {}
        for key, block in raw.items():
            try:
                x, y = (int(p) for p in key.split(",", 1))
            except ValueError:
                continue
            tiles.setdefault((x, y), block)
    return tiles


def _neighbourhood(tiles: dict[Pos, str], center: Pos) -> list[tuple[Pos, str]]:
    cx, cy = center
    out: list[tuple[Pos, str]] = []
    for dx in range(-RADIUS, RADIUS + 1):
        for dy in range(-RADIUS, RADIUS + 1):
            if dx == 0 and dy == 0:
                continue
            p = (cx + dx, cy + dy)
            block = tiles.get(p)
            if block is not None and block not in ("", "?"):
                out.append((p, block))
    return out


def is_odd_block(tiles: dict[Pos, str], pos: Pos) -> bool:
    """True when ``pos`` matches the manual's odd-block motif (M §16)."""
    block = tiles.get(pos)
    if block is None or block not in BREAKABLE:
        return False
    if block in DOORS:
        return False
    same = sum(1 for p, b in tiles.items() if b == block and chebyshev(p, pos) <= RADIUS)
    if same > RARE_MAX:
        return False
    neighbours = _neighbourhood(tiles, pos)
    if len(neighbours) < 8:
        return False
    counts: dict[str, int] = {}
    for _, b in neighbours:
        if b == block:
            continue
        counts[b] = counts.get(b, 0) + 1
    if not counts:
        return False
    dominant = max(counts.values())
    return dominant / len(neighbours) >= DOMINANT_FRAC


def _clue_boost(kb: KnowledgeBase | None, block: str) -> float:
    if kb is None or not block:
        return 0.0
    needle = block.lower()
    boost = 0.0
    with kb.lock:
        clues = list(kb.clues)
    for clue in clues:
        text = (clue.get("text") or "").lower()
        if needle in text:
            boost += 1.0
    return boost


def odd_score(tiles: dict[Pos, str], pos: Pos, clue_boost: float) -> float:
    if not is_odd_block(tiles, pos):
        return 0.0
    return 1.0 + clue_boost


def _danger_near(w: WorldModel, policy, pos: Pos) -> int:
    return sum(
        1
        for e in w.entities
        if e.kind in policy.hostile and chebyshev(e.pos, pos) <= policy.hostile_range
    )


def list_odd_blocks(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy,
    *,
    at: Pos | None = None,
) -> list[OddNomination]:
    if w.map_id is None or w.pos is None:
        return []
    here = at or w.pos
    tiles = _map_tiles(w, kb, w.map_id)
    sr = sight_range(w, w.map_id, here)
    out: list[OddNomination] = []
    for p, block in tiles.items():
        if block not in BREAKABLE:
            continue
        if chebyshev(here, p) > sr:
            continue
        boost = _clue_boost(kb, block)
        score = odd_score(tiles, p, boost)
        if score <= 0:
            continue
        out.append(OddNomination(p, score, boost))
    out.sort(key=lambda n: (-(n.score / (chebyshev(here, n.pos) + 1 + _danger_near(w, policy, n.pos))), n.pos))
    return out


def _choice_at_odd(
    w: WorldModel,
    kb: KnowledgeBase | None,
    pos: Pos,
    nomination: OddNomination,
) -> BreakChoice | None:
    if w.map_id is None:
        return None
    allow_tools = nomination.clue_boost > 0 or nomination.score >= TOOL_MIN_SCORE
    for cap in untried_capabilities(kb, w.map_id, pos, held_capabilities(w, kb)):
        supply = pick_supply_for_capability(w, cap, kb)
        if supply is None:
            continue
        if supply.code not in WEAPONS and not allow_tools:
            continue
        return BreakChoice(pos, cap, supply, break_step_cost(kb, supply.code))
    return None


def pick_odd_break(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy,
    m: Memory,
    *,
    params: dict[str, float | int],
    stick_to: Pos | None = None,
) -> BreakChoice | None:
    """Best odd block we can still try, when curiosity allows (A31)."""
    if w.pos is None or investigate_blocked(w, policy) or w.in_boss_fight():
        return None
    if not curiosity_room(params, m, w.tick):
        return None
    if stick_to is not None:
        tiles = _map_tiles(w, kb, w.map_id)
        block = tiles.get(stick_to)
        if block is not None and block in BREAKABLE:
            boost = _clue_boost(kb, block)
            nom = OddNomination(stick_to, odd_score(tiles, stick_to, boost), boost)
            choice = _choice_at_odd(w, kb, stick_to, nom)
            if choice is not None:
                return choice
        m.break_odd = None
    for nom in list_odd_blocks(w, kb, policy):
        choice = _choice_at_odd(w, kb, nom.pos, nom)
        if choice is not None:
            m.break_odd = nom.pos
            return choice
    m.break_odd = None
    return None

