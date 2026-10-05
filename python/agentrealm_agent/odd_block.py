"""Odd-block detector: nominate breakables that look out of place (A31).

PLAYABLE_AGENT_PLAN Curiosity **Odd-block detector**: one rock in a garden, one
bush in a wheat field. A block is odd when its type is rare in a 7×7
neighbourhood (one or two of it), most surrounding cells share one other type,
and it is breakable in principle. Clue text that names the block type adds a
boost; capability and tool rules live in ``clues`` (A32).
"""

from __future__ import annotations

from dataclasses import dataclass

from .break_memory import (
    BREAKABLE,
    CAPABILITIES,
    WEAPONS,
    _capability_order,
    attempt_failed,
    attempt_open,
    BreakChoice,
    BREAK_BASE_COST,
    break_step_cost,
    held_capabilities,
    pick_supply_for_capability,
    untried_capabilities,
)
from .clues import capability_priority, clue_allows_tools
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


def _window_tiles(
    w: WorldModel, kb: KnowledgeBase | None, map_id: int, center: Pos, radius: int
) -> dict[Pos, str]:
    """Known tiles within ``radius`` of ``center``: the live view first, then knowledge-base terrain.

    Bounded by the window, not the map, so a well-explored map costs no more than a fresh one.
    """
    view = w.maps.get(map_id)
    live = view.tiles if view is not None else w.view.tiles
    cx, cy = center
    cells = [(x, y) for x in range(cx - radius, cx + radius + 1) for y in range(cy - radius, cy + radius + 1)]
    tiles = {p: live[p] for p in cells if p in live}
    if kb is not None and len(tiles) < len(cells):
        with kb.lock:
            raw = (kb.maps.get(str(map_id)) or {}).get("terrain") or {}
            for x, y in cells:
                if (x, y) not in tiles and (block := raw.get(f"{x},{y}")) is not None:
                    tiles[(x, y)] = block
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
    neighbours = _neighbourhood(tiles, pos)
    same = 1 + sum(1 for _, b in neighbours if b == block)
    if same > RARE_MAX:
        return False
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


def _clues_on_map(kb: KnowledgeBase | None, map_id: int) -> list[str]:
    """Lower-cased text of the clues on ``map_id``; clues on other maps do not count."""
    if kb is None:
        return []
    with kb.lock:
        clues = list(kb.clues)
    return [(c.get("text") or "").lower() for c in clues if _clue_on_map(c, map_id)]


def _clue_boost(clue_texts: list[str], block: str) -> float:
    """One per clue that names ``block``."""
    if not block:
        return 0.0
    needle = block.lower()
    return float(sum(1 for text in clue_texts if needle in text))


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
    m: Memory | None = None,
) -> list[OddNomination]:
    """Odd blocks in sight that no break has opened and that are not given up on."""
    if w.map_id is None or w.pos is None:
        return []
    here, map_id = w.pos, w.map_id
    sr = sight_range(w, map_id, here)
    tiles = _window_tiles(w, kb, map_id, here, sr + RADIUS)
    clue_texts = _clues_on_map(kb, map_id)
    out: list[OddNomination] = []
    for p, block in tiles.items():
        if block not in BREAKABLE or chebyshev(here, p) > sr:
            continue
        if _gave_up(m, map_id, p) or already_opened(kb, map_id, p):
            continue
        boost = _clue_boost(clue_texts, block)
        score = odd_score(tiles, p, boost)
        if score > 0:
            out.append(OddNomination(p, score, boost))
    return out


def _choice_at_odd(
    w: WorldModel,
    kb: KnowledgeBase | None,
    nomination: OddNomination,
) -> BreakChoice | None:
    if w.map_id is None:
        return None
    allow_tools = clue_allows_tools(kb, w.map_id, nomination.clue_boost)
    caps = untried_capabilities(kb, w.map_id, nomination.pos, held_capabilities(w, kb))
    caps.sort(key=lambda c: (capability_priority(c, kb, w.map_id), *_capability_order(c)))
    for cap in caps:
        supply = pick_supply_for_capability(w, cap, kb)
        if supply is None:
            continue
        if supply.code not in WEAPONS and not allow_tools:
            continue
        return BreakChoice(nomination.pos, cap, supply, break_step_cost(kb, supply.code))
    return None


def _consumable_cost(choice: BreakChoice) -> int:
    """What a break uses up: nothing for a weapon, one plus the gem price for a tool."""
    if choice.supply.code in WEAPONS:
        return 0
    return 1 + choice.cost - BREAK_BASE_COST


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
    tiles = _window_tiles(w, kb, map_id, pos, RADIUS)
    block = tiles.get(pos)
    if block is None:
        return None
    boost = _clue_boost(_clues_on_map(kb, map_id), block)
    score = odd_score(tiles, pos, boost)
    if score <= 0:
        return None
    return _choice_at_odd(w, kb, OddNomination(pos, score, boost))


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
    """Best odd block we can still try, when curiosity allows (A31). Reads memory, never writes it.

    The sticky target wins while it holds. Otherwise blocks with no failed pair
    come first, then value over distance, danger and what the break uses up
    (PLAYABLE_AGENT_PLAN Curiosity).
    """
    if w.pos is None or w.map_id is None or investigate_blocked(w, policy) or w.in_boss_fight():
        return None
    if not curiosity_room(params, m, w.tick):
        return None
    if stick_to is not None:
        choice = _stick_choice(w, kb, m, stick_to)
        if choice is not None:
            return choice
    here, map_id = w.pos, w.map_id
    ranked: list[tuple[tuple, BreakChoice]] = []
    for nom in list_odd_blocks(w, kb, m):
        choice = _choice_at_odd(w, kb, nom)
        if choice is None:
            continue
        denom = chebyshev(here, nom.pos) + 1 + _danger_near(w, policy, nom.pos) + _consumable_cost(choice)
        ranked.append(((_tried(kb, map_id, nom.pos), -nom.score / denom, nom.pos), choice))
    if not ranked:
        return None
    return min(ranked, key=lambda t: t[0])[1]
