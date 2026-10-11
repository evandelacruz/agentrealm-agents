"""Stalled ops: what the plan remembers about an op that made no progress (A104).

An op whose executor makes no progress for ``plan.PLAN_STALL_SECONDS`` is
moved below the next op on the stack that can make progress
(``Plan.demote_current``), and dropped when none can. Its failure is
remembered here, by target (the op without its ``why``) and why, so a planner
reply that puts it back on top has it moved below again
(``below_progress``) instead of walking into the same wall.

A record holds until something changes that gives a reason to retry
(``retry_reason``): an item held that was not held at the stall (a new
opener), a new clue, or a new way known (a door whose warp is learned, a
level entrance). The planner sees the records in State ``stalled_ops``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .knowledge_base import KnowledgeBase
from .knowledge_maps import door_warp_known
from .world import WorldModel

# The newest stalled ops remembered; an older one is forgotten first.
STALLED_KEPT = 8


@dataclass(frozen=True)
class RetryBasis:
    """What the agent had when an op stalled; more of any is a reason to retry."""

    held: frozenset[str]
    clues: int
    ways: int


@dataclass
class StalledOp:
    """One op that stalled: its target (the op without ``why``), why, when, and what was known then."""

    target: dict[str, Any]
    why: str
    tick: int
    basis: RetryBasis


def target_of(op: dict[str, Any]) -> dict[str, Any]:
    """An op as a target: every field but its free-text ``why``."""
    return {k: v for k, v in op.items() if k != "why"}


def retry_basis(world: WorldModel, knowledge: KnowledgeBase | None) -> RetryBasis:
    """The items carried, the clues and the ways known now."""
    held = {s.code for s in world.held_supplies + world.chest_supplies}
    held.update(c for c in (world.armed_code, *world.worn_codes.values()) if c)
    clues = ways = 0
    if knowledge is not None:
        with knowledge.lock:
            clues = len(knowledge.clues)
            ways = len(knowledge.entrances) + sum(
                1
                for entry in knowledge.maps.values()
                if isinstance(entry, dict)
                for door in entry.get("doors") or []
                if door_warp_known(door)
            )
    return RetryBasis(frozenset(held), clues, ways)


def retry_reason(record: StalledOp, now: RetryBasis) -> str:
    """Why ``record`` may be tried again, or "" while nothing gives a reason."""
    new_items = sorted(now.held - record.basis.held)
    if new_items:
        return f"now holds {', '.join(new_items)}"
    if now.clues > record.basis.clues:
        return "a new clue"
    if now.ways > record.basis.ways:
        return "a new way known"
    return ""


def remember(records: list[StalledOp], op: dict[str, Any], why: str, tick: int, basis: RetryBasis) -> None:
    """Record ``op`` as stalled, replacing an older record of the same target."""
    target = target_of(op)
    records[:] = [r for r in records if r.target != target]
    records.append(StalledOp(target, why, tick, basis))
    del records[:-STALLED_KEPT]


def forget(records: list[StalledOp], op: dict[str, Any]) -> None:
    """``op`` finished: it is no longer a stalled target."""
    target = target_of(op)
    records[:] = [r for r in records if r.target != target]


def find(records: list[StalledOp], op: dict[str, Any]) -> StalledOp | None:
    """The record of ``op``'s target, or None when it has not stalled."""
    target = target_of(op)
    return next((r for r in records if r.target == target), None)


def lapse(records: list[StalledOp], now: RetryBasis) -> list[tuple[StalledOp, str]]:
    """Forget the records something has given a reason to retry; return each with that reason."""
    lapsed = [(r, why) for r in records if (why := retry_reason(r, now))]
    if lapsed:
        gone = {id(r) for r, _ in lapsed}
        records[:] = [r for r in records if id(r) not in gone]
    return lapsed


def below_progress(
    ops: list[dict[str, Any]], stalled: Callable[[dict[str, Any]], bool]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """``ops`` with the stalled ops on top moved just below the first op that can make progress.

    Only the top matters: a stalled op already below an op that can make
    progress stays where it is, and the order is kept otherwise. With no op
    that can make progress, the stalled ops on top leave: nothing could run
    instead of them. Returns the new order, the stalled ops moved or left,
    and the op now above them (None when they left).
    """
    first = next((i for i, op in enumerate(ops) if not stalled(op)), None)
    if first is None:
        return [], list(ops), None
    return [ops[first], *ops[:first], *ops[first + 1 :]], ops[:first], ops[first]
