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
    CAPABILITIES,
    WEAPONS,
    attempt_failed,
    attempt_open,
    BreakChoice,
    break_step_cost,
    held_capabilities,
    pick_supply_for_capability,
    untried_capabilities,
)
from .curiosity_budget import curiosity_room
from .interest_list import MAX_REJECTIONS, investigate_blocked, sight_range
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import DOORS, Pos, WorldModel, chebyshev

RADIUS = 3  # 7×7 neighbourhood
RARE_MAX = 2  # at most this many cells of the same type (including self)
DOMINANT_FRAC = 0.55  # most neighbours share one other type


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


def _clue_on_map(clue: dict, map_id: int) -> bool:
    try:
        return int(clue.get("map_id")) == map_id
    except (TypeError, ValueError):
        return False


def _clue_boost(kb: KnowledgeBase | None, block: str, map_id: int) -> float:
    """One per clue on ``map_id`` that names ``block``; clues on other maps do not count."""
    if kb is None or not block:
        return 0.0
    needle = block.lower()
    boost = 0.0
    with kb.lock:
        clues = list(kb.clues)
    for clue in clues:
        if not _clue_on_map(clue, map_id):
            continue
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


def already_opened(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    """A break opened this cell before, with any capability: a regrown block is not odd again."""
    return any(attempt_open(kb, map_id, pos, cap) for cap in CAPABILITIES)


def _tried(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    return any(attempt_failed(kb, map_id, pos, cap) for cap in CAPABILITIES)


def list_odd_blocks(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy,
    m: Memory | None = None,
) -> list[OddNomination]:
    """Odd blocks in sight, never opened, untried ones first, then by score over distance and danger."""
    if w.map_id is None or w.pos is None:
        return []
    here = w.pos
    tiles = _map_tiles(w, kb, w.map_id)
    sr = sight_range(w, w.map_id, here)
    out: list[OddNomination] = []
    for p, block in tiles.items():
        if block not in BREAKABLE:
            continue
        if chebyshev(here, p) > sr:
            continue
        if _gave_up(m, w.map_id, p) or already_opened(kb, w.map_id, p):
            continue
        boost = _clue_boost(kb, block, w.map_id)
        score = odd_score(tiles, p, boost)
        if score <= 0:
            continue
        out.append(OddNomination(p, score, boost))
    out.sort(
        key=lambda n: (
            _tried(kb, w.map_id, n.pos),
            -(n.score / (chebyshev(here, n.pos) + 1 + _danger_near(w, policy, n.pos))),
            n.pos,
        )
    )
    return out


def _choice_at_odd(
    w: WorldModel,
    kb: KnowledgeBase | None,
    pos: Pos,
    nomination: OddNomination,
) -> BreakChoice | None:
    if w.map_id is None:
        return None
    # Consumable tools only when a clue on this map names the block.
    allow_tools = nomination.clue_boost > 0
    for cap in untried_capabilities(kb, w.map_id, pos, held_capabilities(w, kb)):
        supply = pick_supply_for_capability(w, cap, kb)
        if supply is None:
            continue
        if supply.code not in WEAPONS and not allow_tools:
            continue
        return BreakChoice(pos, cap, supply, break_step_cost(kb, supply.code))
    return None


def _stick_choice(
    w: WorldModel,
    kb: KnowledgeBase | None,
    m: Memory,
    stick_to: tuple[int, Pos],
) -> BreakChoice | None:
    """The sticky target, while it is on this map, in sight, still odd and not given up."""
    map_id, pos = stick_to
    if map_id != w.map_id or w.pos is None or _gave_up(m, map_id, pos) or already_opened(kb, map_id, pos):
        return None
    if chebyshev(w.pos, pos) > sight_range(w, map_id, w.pos):
        return None
    tiles = _map_tiles(w, kb, map_id)
    block = tiles.get(pos)
    if block is None:
        return None
    boost = _clue_boost(kb, block, map_id)
    score = odd_score(tiles, pos, boost)
    if score <= 0:
        return None
    return _choice_at_odd(w, kb, pos, OddNomination(pos, score, boost))


def _gave_up(m: Memory | None, map_id: int, pos: Pos) -> bool:
    return m is not None and m.break_odd_refusals.get((map_id, pos), 0) >= MAX_REJECTIONS


def note_odd_unreachable(m: Memory, map_id: int, pos: Pos) -> None:
    """Break found no route to an odd block: count it, and drop the sticky target."""
    key = (map_id, pos)
    m.break_odd_refusals[key] = m.break_odd_refusals.get(key, 0) + 1
    if m.break_odd == key:
        m.break_odd = None


def pick_odd_break(
    w: WorldModel,
    kb: KnowledgeBase | None,
    policy,
    m: Memory,
    *,
    params: dict[str, float | int],
    stick_to: tuple[int, Pos] | None = None,
) -> BreakChoice | None:
    """Best odd block we can still try, when curiosity allows (A31)."""
    if w.pos is None or w.map_id is None or investigate_blocked(w, policy) or w.in_boss_fight():
        return None
    if not curiosity_room(params, m, w.tick):
        return None
    if stick_to is not None:
        choice = _stick_choice(w, kb, m, stick_to)
        if choice is not None:
            return choice
        m.break_odd = None
    for nom in list_odd_blocks(w, kb, policy, m):
        choice = _choice_at_odd(w, kb, nom.pos, nom)
        if choice is not None:
            m.break_odd = (w.map_id, nom.pos)
            return choice
    m.break_odd = None
    return None

