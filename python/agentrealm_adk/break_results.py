"""Settling a break's ``Use`` (A28), for every agent's loop (A172).

A break sends ``Arm`` (when needed) and a ``Use`` on the block, and files
``Memory.break_pending`` (map, block, capability). The round trip settles it:

- ``note_break_use``, on each intent result: ``applied_no_effect`` marks the
  pair failed; a rejection of that ``Use`` or the ``Arm`` before it marks it
  failed too, unless the refusal was a cooldown (``TRANSIENT_BREAK_REJECTIONS``);
- ``resolve_pending_break``, after the round's events: a ``BlockChanged`` on
  the block marks it opened with the capability and the armed code.

Either way pending is cleared, so the next break picks again. A loop that
replaces a queue still in flight runs ``settle_replaced_break`` on the
queue it replaced.
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


def settle_replaced_break(
    kb: KnowledgeBase | None, m: Memory, w: WorldModel, intents: list[dict] | None, queue: object, start: int,
    results: list[dict], sent: list[dict],
) -> None:
    """Settle a break in flight on a queue just replaced by ``sent`` (A172).

    A ``sent`` that carries the break ``Use`` again owns the pending break,
    so the replaced queue's results leave it alone. Otherwise the replaced
    queue's own results in this response settle it first (``note_break_use``,
    ``intents`` from ``start`` on, under ``queue``); one whose ``Use`` ran stays
    pending, since its ``BlockChanged`` may come in this response
    (``resolve_pending_break``) or a later one; one whose ``Use`` never ran is
    no longer in flight, so a later change there is not ours."""
    if m.break_pending is None or not intents:
        return
    pos = m.break_pending[1]

    def is_break(i: dict | None) -> bool:
        return i is not None and i.get("verb") == "Use" and use_target_block(i, w.entities) == pos

    at = next((n for n in range(start, len(intents)) if is_break(intents[n])), None)
    if at is None or any(is_break(i) for i in sent):
        return
    ran = False
    for res in sorted((x for x in results if x.get("queue_id") == queue), key=lambda x: (x.get("tick", 0), x.get("index", 0))):
        n = int(res.get("index", 0))
        if n < start or n >= len(intents) or m.break_pending is None:
            continue
        intent = intents[n]
        block = use_target_block(intent, w.entities) if intent.get("verb") == "Use" else None
        note_break_use(kb, m, w, intent, res, block, list(intents[n:]))
        ran |= n == at and res.get("outcome") != "rejected"
    if m.break_pending is not None and not ran:
        m.break_pending = None
