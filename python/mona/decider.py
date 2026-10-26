"""The goal decider: one of four modes, always (docs/v4/SPEC.md **The goal decider**).

Deterministic. It is run only when the current mode is met, after a death,
or when the strategist changes the kit; between those moments the mode is
fixed.
"""

from __future__ import annotations

from enum import Enum

from adk3.realm.state import State

from .kit import Kit


class Mode(Enum):
    LEARN = "learn what level N needs"
    MONEY = "get money"
    GEAR = "get gear"
    BEAT = "beat level N"


def next_level(s: State) -> int:
    """The lowest level not yet cleared."""
    n = 1
    while n in s.levels_cleared:
        n += 1
    return n


def decide(s: State, kit: Kit | None) -> Mode:
    if kit is None or kit.level != next_level(s) or not kit.trusted:
        return Mode.LEARN
    if s.gems < kit.cost(s):
        return Mode.MONEY
    if kit.missing(s):
        return Mode.GEAR
    return Mode.BEAT
