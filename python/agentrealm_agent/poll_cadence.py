"""When to spend a window on POST tick vs reads or silence (M6).

Calm: poll every 4–10 ticks and leave the windows between to the reads the
scheduler already prioritises. Urgent: poll every tick while a hostile is
within 3 blocks or health is dropping (docs/PLAYABLE_AGENT_PLAN.md Executor).

The calm gap never outlasts the queue the last poll sent: a queue of n
intents runs n ticks, and the character must not stand idle after it. Today's
queues hold one intent, so a character with something to do still polls every
tick; the gap opens only when the last poll sent nothing, and widens on its
own once multi-intent queues land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Policy
from .world import WorldModel, chebyshev

if TYPE_CHECKING:
    from .brain import Memory

THREAT_NEAR_BLOCKS = 3
CALM_POLL_MIN = 4
CALM_POLL_MAX = 10


def calm_poll_interval(last_poll_tick: int, character_id: int) -> int:
    """Deterministic spacing in [CALM_POLL_MIN, CALM_POLL_MAX] after a poll."""
    n = (max(0, last_poll_tick) * 3 + character_id) % (CALM_POLL_MAX - CALM_POLL_MIN + 1)
    return CALM_POLL_MIN + n


def hostile_within(w: WorldModel, policy: Policy, blocks: int = THREAT_NEAR_BLOCKS) -> bool:
    """From the last entity read, which the scheduler refreshes between polls."""
    if w.pos is None:
        return False
    return any(e.kind in policy.hostile and chebyshev(e.pos, w.pos) <= blocks for e in w.entities)


def is_urgent(w: WorldModel, m: Memory, policy: Policy) -> bool:
    """Hostile near, or health dropping: Damaged or Attacked not yet re-read
    (alarm), or Damaged in the last poll's events."""
    return m.alarm or m.hurt_last_poll or hostile_within(w, policy)


def gate_tick_call(w: WorldModel, m: Memory, policy: Policy) -> str:
    """tick, or skip to leave this window unspent until the calm gap is up."""
    if m.last_poll_tick < 0 or is_urgent(w, m, policy):
        return "tick"
    gap = m.calm_poll_interval
    if m.queued_ticks > 0:
        gap = min(gap, m.queued_ticks)
    return "tick" if w.tick - m.last_poll_tick >= gap else "skip"
