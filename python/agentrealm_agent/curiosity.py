"""Curiosity budget tracking (A30). Enforcement against ``curiosity`` × window is A32."""

from __future__ import annotations

from .memory import Memory

WINDOW_TICKS = 600  # 60 s at 10 Hz (PLAYABLE_AGENT_PLAN Curiosity)


def record_curiosity_queue(m: Memory, start_tick: int, queue_ticks: int) -> None:
    """Remember ticks a curiosity queue (Investigate or Break walk) will cover."""
    if queue_ticks <= 0:
        return
    m.curiosity_segments.append((start_tick, queue_ticks))


def curiosity_ticks_used(m: Memory, now_tick: int, *, window: int = WINDOW_TICKS) -> int:
    """Ticks covered by Investigate/Break queues in the rolling window."""
    cutoff = now_tick - window
    total = 0
    kept: list[tuple[int, int]] = []
    for start, length in m.curiosity_segments:
        if start + length <= cutoff:
            continue
        overlap_start = max(start, cutoff)
        total += start + length - overlap_start
        kept.append((start, length))
    m.curiosity_segments = kept
    return total


def curiosity_share_used(m: Memory, now_tick: int, curiosity: float, *, window: int = WINDOW_TICKS) -> float:
    """Fraction of the window spent on curiosity queues (0–1+ before cap enforcement)."""
    if window <= 0:
        return 0.0
    return curiosity_ticks_used(m, now_tick, window=window) / window


def detour_allowed(m: Memory, now_tick: int, curiosity: float, *, window: int = WINDOW_TICKS) -> bool:
    """Whether another Investigate/Break detour fits under the budget (A32 enforcement)."""
    cap = curiosity * window
    return curiosity_ticks_used(m, now_tick, window=window) < cap
