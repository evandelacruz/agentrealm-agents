"""Break memory per (block, capability) and grid break costs (A28).

A supply's capabilities come only from sourced facts: the ones the Manual's
Supplies reference lists in its ``use_effects`` (A54, ``supplies``), plus any
capability a break with that subtype armed has opened (A46). Both are filed on
its ``items`` row; the reference is also read directly, so a caller with no
knowledge base still sees them. A code the reference does not list carries
none until it opens a block. Break attempts are stored with keys
``"<map_id>,<x>,<y>,<capability>"``.

A block is tried only with a capability known to open it (A103): one its
type's documented rule names (``BLOCK_OPENERS``), or one that opened that
cell before. A capability that failed on a cell is not tried there again.
A block with no known opener among the held items is not nominated, so the
walk routes around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from .executor.pacing import use_cooldown_ticks
from .item_table import InventorySupply, merge_capability
from .knowledge_base import KnowledgeBase
from .supplies import CAPABILITIES, break_capabilities, kept_on_break, listed
from .world import Pos, WorldModel

if TYPE_CHECKING:
    from .navigation.stuck import NavStuckMemory

# The ``Memory.goal`` of every walk to a block to break it: Break's, for the
# plan's op or a stuck ladder's step 2, and Heal's cut toward food.
BREAK_GOAL = "break"

BREAKABLE = frozenset({"bush", "tree", "rock", "mountain", "wall"})
REGROWTH_TICKS = 600  # 60 s at 10 Hz: a block's recovery time unless the world sets another (Manual §11 Regrowth)
# A break's own tick, the ``Use``. A weapon's break adds its attack cooldown;
# a tool's (matches, a bomb) waits on none (Manual §11 What waits on a
# cooldown, A153).
BREAK_BASE_COST = 1
# The shape of a break's ``Use``: a block target, for ``use_cooldown_ticks``.
_BREAK_USE = {"verb": "Use", "target": {"kind": "block"}}
# Refusals of a break that say "not yet", not "cannot": the pair is not marked failed.
TRANSIENT_BREAK_REJECTIONS = frozenset({"attack_cooldown"})
# Results that mark a (block, capability) pair failed: it is not tried there again.
FAILED_RESULTS = frozenset({"applied_no_effect", "failed", "no_effect"})

# What opens each breakable block type, by the game's rules (Manual §11
# Breaking blocks): the knife and swords cut bushes, only a sword chops trees,
# a mallet smashes rocks, matches or a torch burn hedge trees and a few bushes,
# and a bomb blasts rocks, mountain and a few walls (a hidden entrance becomes
# a door). Grass is cut too, but it is walkable ground, which Gather cuts and
# Break never takes. An odd block may need any of the five, so Break holds
# an oddity to none of these (A160).
BLOCK_OPENERS: dict[str, frozenset[str]] = {
    "bush": frozenset({"cut", "burn"}),
    "tree": frozenset({"chop", "burn"}),
    "rock": frozenset({"smash", "blast"}),
    "mountain": frozenset({"blast"}),
    "wall": frozenset({"blast"}),
}


def break_key(map_id: int, pos: Pos, capability: str) -> str:
    return f"{map_id},{pos[0]},{pos[1]},{capability}"


def capabilities_for_code(code: str, kb: KnowledgeBase | None = None) -> frozenset[str]:
    """Sourced capabilities of subtype ``code``: the Supplies reference's plus
    those its ``items`` row holds (filed from the reference, or a break it opened)."""
    if not code:
        return frozenset()
    caps = set(break_capabilities(code))
    if kb is not None:
        with kb.lock:
            row = kb.items.get(code)
            learned = list(row.get("capabilities") or []) if isinstance(row, dict) else []
        caps.update(c for c in learned if c in CAPABILITIES)
    return frozenset(caps)


def cannot_cut(code: str | None, kb: KnowledgeBase | None = None, capability: str = "cut") -> bool:
    """Sourced facts say ``code`` does not cut (or does not have
    ``capability``, ``chop`` for a tree): the Supplies reference lists it
    without it (food, a potion, a mallet, matches; the knife for ``chop``), or
    its known capabilities leave it out.

    A code the reference does not list, or an empty hand, may still cut: only
    a known miss is ruled out.
    """
    if not code:
        return False
    caps = capabilities_for_code(code, kb)
    if listed(code):
        return capability not in caps
    return bool(caps) and capability not in caps


def _break_row(kb: KnowledgeBase | None, key: str) -> dict[str, Any] | None:
    if kb is None:
        return None
    with kb.lock:
        row = kb.breaks.get(key)
    return row if isinstance(row, dict) else None


def attempt_failed(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, capability: str) -> bool:
    if map_id is None:
        return False
    row = _break_row(kb, break_key(map_id, pos, capability))
    if row is None:
        return False
    return row.get("result") in FAILED_RESULTS


def attempt_open(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, capability: str) -> bool:
    if map_id is None:
        return False
    row = _break_row(kb, break_key(map_id, pos, capability))
    return isinstance(row, dict) and row.get("result") == "opened"


def attempts_at(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, since: int) -> list[tuple[str, str]]:
    """Each capability a break of ``pos`` was recorded with at tick ``since``
    or later, and its result. The records are kept across runs, so an older
    one, or one with no tick, is someone else's try."""
    if map_id is None:
        return []
    rows = ((c, _break_row(kb, break_key(map_id, pos, c))) for c in sorted(CAPABILITIES))
    return [
        (c, str(row.get("result")))
        for c, row in rows
        if row is not None and isinstance(row.get("tick"), int) and row["tick"] >= since
    ]


def known_openers(w: WorldModel, kb: KnowledgeBase | None, pos: Pos) -> frozenset[str]:
    """Capabilities known to open the block at ``pos``: those its type's rule
    names (``BLOCK_OPENERS``), plus any that opened this cell before."""
    caps = set(BLOCK_OPENERS.get(w.view.tiles.get(pos, ""), ()))
    caps.update(c for c in CAPABILITIES if attempt_open(kb, w.map_id, pos, c))
    return frozenset(caps)


def record_attempt(
    kb: KnowledgeBase | None,
    *,
    map_id: int | None,
    pos: Pos,
    capability: str,
    result: str,
    block_after: str | None = None,
    tick: int | None = None,
    code: str | None = None,
) -> None:
    if kb is None or map_id is None or capability not in CAPABILITIES:
        return
    entry: dict[str, Any] = {"result": result, "capability": capability}
    if code:
        entry["code"] = code
    if block_after is not None:
        entry["block_after"] = block_after
    if tick is not None:
        entry["tick"] = tick
    with kb.lock:
        kb.breaks[break_key(map_id, pos, capability)] = entry
        if result == "opened" and code:
            # The breaks row is overwritten by the next try on this block; the
            # item row keeps what the subtype has proven it can do (A46).
            merge_capability(kb.items, code, capability)


def held_supplies(w: WorldModel) -> list[InventorySupply]:
    """Supplies Break can arm: held, plus the armed one. Worn gear is not armable."""
    out = list(w.held_supplies)
    if w.armed_code and not any(s.code == w.armed_code for s in w.held_supplies):
        out.append(InventorySupply(-1, w.armed_code))
    return out


def held_supply(w: WorldModel, code: str) -> InventorySupply | None:
    """A supply of ``code`` in hand. One only in the carried chest does not count:
    Solve does not withdraw it, so the op stalls and is dropped (A39)."""
    for s in w.held_supplies:
        if s.code == code:
            return s
    return None


def held_capabilities(w: WorldModel, kb: KnowledgeBase | None = None) -> set[str]:
    caps: set[str] = set()
    for s in held_supplies(w):
        caps |= capabilities_for_code(s.code, kb)
    return caps


@dataclass(frozen=True)
class BreakChoice:
    pos: Pos
    capability: str
    supply: InventorySupply
    cost: int


def _tool_gem_cost(kb: KnowledgeBase | None, code: str) -> int:
    if kb is None or not code:
        return 0
    with kb.lock:
        row = kb.items.get(code)
    if not isinstance(row, dict):
        return 0
    price = row.get("gem_price")
    return int(price) if isinstance(price, int) and not isinstance(price, bool) and price > 0 else 0


def _capability_order(cap: str) -> tuple[int, str]:
    # Weapons (free) before consumable tools; cut before smash before burn before blast.
    order = {"cut": 0, "chop": 1, "smash": 2, "burn": 3, "blast": 4}
    return order.get(cap, 9), cap


def pick_supply_for_capability(
    w: WorldModel, capability: str, kb: KnowledgeBase | None = None
) -> InventorySupply | None:
    """The held or armed supply to use for ``capability``: one a break does not
    use up first, the armed one on a tie, then by code.

    The armed supply wins a tie, so a break arms its tool once and keeps it.
    An armed supply is not in ``held``: without the tie-break, two weapons
    that both cut would each pick the other and flip the arm forever.
    """
    cands: list[tuple[tuple[int, int, str], InventorySupply]] = []
    for s in held_supplies(w):
        if capability not in capabilities_for_code(s.code, kb):
            continue
        used_up = 0 if kept_on_break(s.code) else 1
        armed = 0 if s.code == w.armed_code else 1
        cands.append(((used_up, armed, s.code), s))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def untried_capabilities(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, held: Iterable[str]) -> list[str]:
    out = [c for c in held if c in CAPABILITIES and not attempt_failed(kb, map_id, pos, c)]
    out.sort(key=_capability_order)
    return out


def break_step_cost(kb: KnowledgeBase | None, code: str) -> int:
    """The ``Use``'s tick, plus a weapon's attack cooldown (none for a tool,
    ``use_cooldown_ticks``), plus the gem price of a tool a break uses up
    (any supply the reference does not list as kept on a break)."""
    cooldown = use_cooldown_ticks(_BREAK_USE, code)
    return BREAK_BASE_COST + cooldown + (0 if kept_on_break(code) else _tool_gem_cost(kb, code))


def breakable_block(w: WorldModel, pos: Pos) -> str | None:
    block = w.view.tiles.get(pos)
    if block in BREAKABLE:
        return block
    return None


def _step_toward(p: Pos, goal: Pos) -> Pos:
    nx = p[0] + (1 if goal[0] > p[0] else -1 if goal[0] < p[0] else 0)
    ny = p[1] + (1 if goal[1] > p[1] else -1 if goal[1] < p[1] else 0)
    return nx, ny


def _route(start: Pos, goal: Pos) -> Iterator[Pos]:
    """The cells of the straight route from ``start`` to ``goal``, ``start`` left out."""
    p = start
    while p != goal:
        p = _step_toward(p, goal)
        yield p


def straight_route(start: Pos, goal: Pos) -> list[Pos]:
    """The cells of the straight route from ``start`` to ``goal``, ``start`` left out."""
    return list(_route(start, goal))


def choice_at(
    w: WorldModel, kb: KnowledgeBase | None, pos: Pos, skip: frozenset[Pos] = frozenset()
) -> BreakChoice | None:
    """How to open the block at ``pos`` with what is held (``_choice_at``),
    for a walk whose priced way crosses that block."""
    return _choice_at(w, kb, pos, held_capabilities(w, kb), skip)


def _choice_at(
    w: WorldModel, kb: KnowledgeBase | None, pos: Pos, held: set[str], skip: frozenset[Pos] = frozenset()
) -> BreakChoice | None:
    """The cheapest way to open ``pos`` with what is held: a capability known
    to open it (``known_openers``) that has not failed there. None for a
    cell in ``skip``: a block given up (``given_up_blocks``)."""
    if w.map_id is None or pos in skip or breakable_block(w, pos) is None:
        return None
    for cap in untried_capabilities(kb, w.map_id, pos, held & known_openers(w, kb, pos)):
        supply = pick_supply_for_capability(w, cap, kb)
        if supply is None:
            continue
        return BreakChoice(pos, cap, supply, break_step_cost(kb, supply.code))
    return None


def no_break_reason(
    w: WorldModel, kb: KnowledgeBase | None, start: Pos, goal: Pos, skip: frozenset[Pos] = frozenset()
) -> str:
    """Why ``nominate_on_path`` found nothing, for stuck escalation's reasons:
    ``no_known_opener`` when a breakable stands on the route that nothing held
    is known to open (or every known opener failed on), ``given_up_break``
    when the only ones were given up (``skip``), else ``no_break``."""
    held = held_capabilities(w, kb)
    reason = "no_break"
    for p in _route(start, goal):
        if breakable_block(w, p) is None:
            continue
        if p in skip:
            reason = "given_up_break"
        elif _choice_at(w, kb, p, held) is None:
            return "no_known_opener"
    return reason


def nominate_on_path(
    w: WorldModel,
    kb: KnowledgeBase | None,
    start: Pos,
    goal: Pos,
    skip: frozenset[Pos] = frozenset(),
) -> BreakChoice | None:
    """The first breakable on the straight route from ``start`` to ``goal`` we can still try.

    Only cells on that route are candidates: a breakable beside or behind us
    does not open the way to ``goal``. A block no held item is known to open,
    or one in ``skip`` (given up), is passed over (``_choice_at``).
    """
    held = held_capabilities(w, kb)
    if not held or w.map_id is None:
        return None
    for p in _route(start, goal):
        choice = _choice_at(w, kb, p, held, skip)
        if choice is not None:
            return choice
    return None


def break_costs_for_planning(
    w: WorldModel,
    kb: KnowledgeBase | None,
    goal: Pos | None,
    skip: frozenset[Pos] = frozenset(),
) -> dict[Pos, int]:
    """Break step costs for breakables on the straight route to ``goal`` that a
    held item is known to open with a capability not yet failed there, and
    that are not in ``skip`` (given up)."""
    if w.map_id is None or w.pos is None:
        return {}
    held = held_capabilities(w, kb)
    if not held:
        return {}
    cells: set[Pos] = set()
    if goal is not None:
        cells.update(p for p in _route(w.pos, goal) if breakable_block(w, p) is not None)
    costs: dict[Pos, int] = {}
    for p in cells:
        choice = _choice_at(w, kb, p, held, skip)
        if choice is not None:
            costs[p] = choice.cost
    return costs


def given_up_blocks(stuck: NavStuckMemory, map_id: int | None, tick: int) -> frozenset[Pos]:
    """The blocks on ``map_id`` a walk to break them was given up on and is
    still backed off from: no ladder, cost grid or Heal walk nominates them
    until the backoff ends (A90)."""
    if map_id is None:
        return frozenset()
    return frozenset(
        r.target for r in stuck.given_up.values() if r.goal == BREAK_GOAL and r.map_id == map_id and tick < r.until
    )
