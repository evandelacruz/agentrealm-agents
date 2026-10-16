"""Reach: how far an intent acts from where the character stands (A116).

Some intents act only within reach of the block the character stands on
when the intent runs (API rules):

- ``Take``, ``WithdrawFromChest`` and ``DepositToChest``: the target's
  block or a neighbouring one, corners included (``NEAR``).
- ``Use`` on a block, an NPC or a character: the armed weapon's attack
  range, or one block for a tool.

Such an intent is sent only from a position the server has confirmed within
reach (``in_reach_from``). One rule for every reach-limited intent: a new
one goes in ``intent_reach``, never in a state's own check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from .item_table import use_target_block
from .supplies import attack_reach
from .world import Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

NEAR = 1  # Take, WithdrawFromChest, DepositToChest, and a tool's Use (API)


def supply_reach(knowledge: KnowledgeBase | None, code: str | None) -> int:
    """A supply's ``Use`` reach once armed: its learned ``attack_range``, else
    a weapon's published one (``supplies.attack_reach``, A123), else a tool's
    one block."""
    if knowledge is not None and code:
        row = knowledge.items.get(code)
        if isinstance(row, dict):
            reach = row.get("attack_range")
            if isinstance(reach, int) and not isinstance(reach, bool) and reach > 0:
                return reach
    return attack_reach(code) or NEAR


def weapon_reach(w: WorldModel, knowledge: KnowledgeBase | None) -> int:
    """The armed supply's ``Use`` reach: ``get_self``'s ``attack_range``, else
    ``supply_reach``."""
    if w.attack_range is not None:
        return w.attack_range
    return supply_reach(knowledge, w.armed_code)


def intent_reach(
    intent: dict, w: WorldModel, knowledge: KnowledgeBase | None, armed_code: str | None = None
) -> tuple[Pos, int] | None:
    """The block a reach-limited intent acts on and its reach, or None for an
    intent with no reach limit (a Step, an Arm, a Use on yourself) or a target
    we cannot place. ``armed_code`` is a supply an earlier ``Arm`` of the same
    queue arms first; None means what is armed now."""
    verb = intent.get("verb")
    if verb == "Take":
        pos = _entity_pos(w, "supply", intent.get("supply_id"))
        return (pos, NEAR) if pos is not None else None
    if verb in ("WithdrawFromChest", "DepositToChest"):
        pos = _entity_pos(w, "chest", intent.get("chest_id"))
        return (pos, NEAR) if pos is not None else None
    if verb == "Use":
        pos = use_target_block(intent, w.entities)
        if pos is None:
            return None
        reach = supply_reach(knowledge, armed_code) if armed_code is not None else weapon_reach(w, knowledge)
        return pos, reach
    return None


def in_reach_from(
    intents: Iterable[dict], starts: list[Pos], w: WorldModel, knowledge: KnowledgeBase | None
) -> bool:
    """Every reach-limited intent in ``intents`` reaches its target from each
    of ``starts``, the cells the character may stand on when it runs."""
    armed_code: str | None = None
    for intent in intents:
        if intent.get("verb") == "Arm":
            armed_code = _held_code(w, intent.get("supply_id")) or ""  # unknown: a tool's reach
            continue
        target = intent_reach(intent, w, knowledge, armed_code)
        if target is not None and any(chebyshev(p, target[0]) > target[1] for p in starts):
            return False
    return True


def _entity_pos(w: WorldModel, kind: str, eid: object) -> Pos | None:
    return next((e.pos for e in w.entities if e.kind == kind and e.id == eid), None)


def _held_code(w: WorldModel, supply_id: object) -> str | None:
    code = next((s.code for s in w.held_supplies if s.id == supply_id), None)
    return code if code is not None else (w.armed_code if supply_id == w.armed_id else None)
