"""Curiosity budget (A30, PLAYABLE_AGENT_PLAN Curiosity).

Over the last 600 ticks, the ticks covered by queues ``Investigate`` sent may
not exceed ``curiosity`` × 600. Reads and speech from where the agent stands
are free and never charged; only movement queues (detours) are. The runner
charges them (``record_curiosity_queue``) and ``pick_interest_tick`` gates
detour items on ``detour_allowed``. A30 nominates no detour yet (door and
entrance looks are deferred, PLAN.md A30), so the gate waits on those and on
``Break`` (A28).
"""

from __future__ import annotations

from .memory import Memory

WINDOW_TICKS = 600  # 60 s at 10 Hz (PLAYABLE_AGENT_PLAN Curiosity)


def record_curiosity_queue(m: Memory, start_tick: int, queue_ticks: int, *, window: int = WINDOW_TICKS) -> None:
    """Charge the ticks a detour queue covers, dropping segments out of the window."""
    if queue_ticks <= 0:
        return
    cutoff = start_tick - window
    m.curiosity_segments = [(s, n) for s, n in m.curiosity_segments if s + n > cutoff]
    m.curiosity_segments.append((start_tick, queue_ticks))


def curiosity_ticks_used(m: Memory, now_tick: int, *, window: int = WINDOW_TICKS) -> int:
    """Charged ticks inside the window ending at ``now_tick``."""
    cutoff = now_tick - window
    return sum(s + n - max(s, cutoff) for s, n in m.curiosity_segments if s + n > cutoff)


def detour_allowed(m: Memory, now_tick: int, curiosity: float, *, window: int = WINDOW_TICKS) -> bool:
    """Whether another detour fits under ``curiosity`` × window."""
    return curiosity_ticks_used(m, now_tick, window=window) < curiosity * window
