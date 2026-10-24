"""Settling a break's ``Use`` (A28), for every agent's loop (A172).

A break sends ``Arm`` (when needed) and a ``Use`` on the block, and files
``Memory.break_pending`` (map, block, capability). The round trip settles it:

- ``note_break_use``, on each intent result: ``applied_no_effect`` marks the
  pair failed; a rejection of that ``Use`` or the ``Arm`` before it marks it
  failed too, unless the refusal was a cooldown (``TRANSIENT_BREAK_REJECTIONS``);
- ``resolve_pending_break``, after the round's events: a ``BlockChanged`` on
  the block marks it opened with the capability and the armed code.

Either way pending is cleared, so the next break picks again.
"""

from __future__ import annotations

from .break_memory import TRANSIENT_BREAK_REJECTIONS, record_attempt
from .item_table import use_target_block
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation.stuck import active, on_break_opened, on_break_tried
from .world import Pos, WorldModel


def note_break_use(
    kb: KnowledgeBase | None, m: Memory, w: WorldModel, intent: dict | None, result: dict, block: Pos | None,
    ahead: list[dict | None],
) -> None:
    """Settle the break ``Use`` in flight on ``intent``'s ``result``.
    ``block`` is the block ``intent`` targets; ``ahead`` the intents from
    this one on in its queue, so a rejection past the break ``Use`` is not its."""
    pending = m.break_pending
    if pending is None:
        return
    map_id, pos, cap = pending
    tick = int(result.get("tick", w.tick))
    outcome = result.get("outcome")
    if outcome == "rejected":
        if not any(i is not None and i.get("verb") == "Use" and use_target_block(i, w.entities) == pos for i in ahead):
            return  # the break Use already ran: its BlockChanged may still come
        m.break_pending = None
        code = (result.get("rejection") or {}).get("code")
        verb = (intent or {}).get("verb")
        ours = verb == "Arm" or (verb == "Use" and block == pos)
        if ours and code not in TRANSIENT_BREAK_REJECTIONS:
            record_attempt(kb, map_id=map_id, pos=pos, capability=cap, result="failed", tick=tick)
            on_break_tried(m, w)
        return
    if intent is None or intent.get("verb") != "Use" or block != pos:
        return
    if outcome == "applied_no_effect":
        record_attempt(kb, map_id=map_id, pos=pos, capability=cap, result="applied_no_effect", tick=tick)
        m.break_pending = None
        on_break_tried(m, w)


def resolve_pending_break(kb: KnowledgeBase | None, m: Memory, w: WorldModel) -> None:
    """A ``BlockChanged`` on the pending block this round: the break opened it."""
    pending = m.break_pending
    if pending is None:
        return
    map_id, pos, cap = pending
    if not w.alive or w.map_id != map_id:
        # Died or left the map: a later change at that cell is not our break.
        m.break_pending = None
        return
    for changed_map, p in w.changed_blocks:
        if changed_map == map_id and p == pos:
            record_attempt(
                kb, map_id=map_id, pos=pos, capability=cap, result="opened",
                block_after=w.view.tiles.get(pos, ""), tick=w.tick, code=w.armed_code,
            )
            m.break_pending = None
            on_break_opened(m, w, active(m, w))
            return
