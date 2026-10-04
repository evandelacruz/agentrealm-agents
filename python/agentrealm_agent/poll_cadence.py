"""When to spend a window on POST tick vs reads or silence (M6).

Calm: poll every 4–10 ticks and use other windows on reads the scheduler
already prioritises. Urgent: poll every tick while a hostile is within 3
blocks or health is dropping (docs/PLAYABLE_AGENT_PLAN.md Executor).
"""

from __future__ import annotations

from .config import Policy
from .world import WorldModel, chebyshev

THREAT_NEAR_BLOCKS = 3
CALM_POLL_MIN = 4
CALM_POLL_MAX = 10


def calm_poll_interval(last_poll_tick: int, character_id: int) -> int:
    """Deterministic spacing in [CALM_POLL_MIN, CALM_POLL_MAX] after a poll."""
    n = (max(0, last_poll_tick) * 7 + character_id) % (CALM_POLL_MAX - CALM_POLL_MIN + 1)
    return CALM_POLL_MIN + n


def hostile_within(w: WorldModel, policy: Policy, blocks: int = THREAT_NEAR_BLOCKS) -> bool:
    if w.pos is None:
        return False
    here = w.pos
    for e in w.entities:
        if e.kind in policy.hostile and chebyshev(e.pos, here) <= blocks:
            return True
    return False


def health_dropping(w: WorldModel, *, alarm: bool, last_poll_tick: int, prev_health: int | None, health: int | None) -> bool:
    if alarm:
        return True
    if health is not None and prev_health is not None and health < prev_health:
        return True
    if w.tick > last_poll_tick and w.damage_since(last_poll_tick + 1) > 0:
        return True
    return False


def is_urgent(w: WorldModel, policy: Policy, *, alarm: bool, last_poll_tick: int, prev_health: int | None, health: int | None) -> bool:
    if health_dropping(w, alarm=alarm, last_poll_tick=last_poll_tick, prev_health=prev_health, health=health):
        return True
    return hostile_within(w, policy)


def should_poll_tick(
    w: WorldModel,
    policy: Policy,
    *,
    alarm: bool,
    last_poll_tick: int,
    calm_interval: int,
    prev_health: int | None,
    health: int | None,
) -> bool:
    """True when this window should POST tick rather than skip after reads."""
    if is_urgent(w, policy, alarm=alarm, last_poll_tick=last_poll_tick, prev_health=prev_health, health=health):
        return True
    if last_poll_tick < 0:
        return True
    return w.tick - last_poll_tick >= calm_interval


def gate_tick_call(
    w: WorldModel,
    policy: Policy,
    *,
    alarm: bool,
    last_poll_tick: int,
    calm_interval: int,
    prev_health: int | None,
    health: int | None,
) -> str:
    """Returns tick or skip when the scheduler would otherwise POST tick."""
    if should_poll_tick(
        w,
        policy,
        alarm=alarm,
        last_poll_tick=last_poll_tick,
        calm_interval=calm_interval,
        prev_health=prev_health,
        health=health,
    ):
        return "tick"
    return "skip"
