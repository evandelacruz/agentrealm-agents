"""Rolling curiosity budget for Investigate and Break (A30, A31, PLAYABLE_AGENT_PLAN Curiosity).

Over the last 600 ticks, queues that Investigate or Break send may cover at most
``curiosity`` × 600 ticks. Reads and speech from where the agent stands are free
and are not counted toward the cap.
"""

from __future__ import annotations

from .directives import PARAM_DEFAULTS
from .memory import Memory

WINDOW_TICKS = 600
CURIOSITY_STATES = frozenset({"Investigate", "Break", "OddBreak"})


def cap_ticks(curiosity: float) -> int:
    return max(0, int(curiosity * WINDOW_TICKS))


def _window_start(now: int) -> int:
    return now - WINDOW_TICKS + 1


def ticks_in_window(spans: list[tuple[int, int]], now: int) -> int:
    """How many tick-slots charged curiosity queues cover in the rolling window."""
    if now < 1:
        return 0
    wstart = _window_start(now)
    wend = now + 1
    total = 0
    for start, length in spans:
        if length <= 0:
            continue
        qend = start + length
        total += max(0, min(qend, wend) - max(start, wstart))
    return total


def prune_spans(spans: list[tuple[int, int]], now: int) -> list[tuple[int, int]]:
    """Drop spans that cannot overlap the window anymore."""
    cutoff = _window_start(now) - WINDOW_TICKS
    return [(s, n) for s, n in spans if s + n > cutoff]


def curiosity_room(params: dict[str, float | int], m: Memory, now: int) -> bool:
    curiosity = float(params.get("curiosity", PARAM_DEFAULTS["curiosity"]))
    used = ticks_in_window(m.curiosity_spans, now)
    return used < cap_ticks(curiosity)


def charged_ticks_in_queue(intents: list[dict] | None) -> int:
    """Queue length that counts against curiosity: everything but Read/Say/Broadcast."""
    if not intents:
        return 0
    n = 0
    for intent in intents:
        if intent.get("verb") in ("Read", "Say", "Broadcast"):
            continue
        n += 1
    return n


def record_curiosity_queue(m: Memory, start_tick: int, intents: list[dict] | None, state: str) -> None:
    if state not in CURIOSITY_STATES:
        return
    n = charged_ticks_in_queue(intents)
    if n <= 0:
        return
    m.curiosity_spans = prune_spans(m.curiosity_spans, start_tick)
    m.curiosity_spans.append((start_tick, n))
