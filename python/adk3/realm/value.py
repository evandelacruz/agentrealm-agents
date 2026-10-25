"""What things are worth, in gems: the one unit every choice is priced in (docs/v3/SPEC.md **Engines**).

Gems are the game's currency (Manual §16), so health, time, a life, an item
and a clue are each priced in gems, and a fight, a detour or a pickup is
reward less cost on one scale. The **levers** are an agent's temperament:
a few numbers from its profile, each feeding one of these prices. Two
agents on this framework differ in their levers, not their code.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from agentrealm_adk import supplies

POTIONS = ("small_potion", "potion", "large_potion")


@dataclass(frozen=True)
class Levers:
    risk: float = 5  # 0 to 10: how lightly a chance of death weighs; 0 very cautious, 10 reckless
    life_worth: float = 40  # gems: what losing a life costs, besides the gear a death drops
    time_worth: float = 6  # gems per minute: the Manual's field-work rate (§16)
    curiosity: float = 5  # 0 to 10: how much a sign, a helper or unseen ground is worth stopping for
    greed: float = 5  # 0 to 10: how much loot in view is worth stopping for
    heal_below: float = 0.4  # share of health: below it, heal before going on
    patience: float = 60  # seconds a skill may make no progress before it has failed

    @classmethod
    def of(cls, raw: dict[str, Any] | None) -> Levers:
        known = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in (raw or {}).items() if k in known})

    @property
    def death_weight(self) -> float:
        """How a death's cost is weighed: 1 at risk 5, about 1.8 at 0, about 0.2 at 10."""
        return (11 - self.risk) / 6

    @property
    def curiosity_weight(self) -> float:
        return self.curiosity / 5

    @property
    def greed_weight(self) -> float:
        return self.greed / 5


WORLD: str | None = None  # the world played: prices are that world's (``use_world``)


def use_world(code: str) -> None:
    """Price everything in this world's gems: the reference lists a price per world, and the cheapest across
    worlds (a sandbox potion for 2) undervalues everything here."""
    global WORLD
    WORLD = code


def item(code: str) -> float:
    """An item's worth: its listed price in the world played (the Supplies reference), 0 when it is not sold there."""
    return float(supplies.list_price(code, WORLD) or 0)


def health() -> float:
    """One health, at the cheapest potion's price per health it gives back (Manual §11, §16)."""
    rates = [item(c) / supplies.heal_of(c) for c in POTIONS if item(c) and supplies.heal_of(c)]
    return min(rates) if rates else 1.0


def health_change(amount: float, now: float, max_health: float) -> float:
    """What ``amount`` health gained (or, below 0, lost) is worth at ``now`` health: the potion price per health,
    scaled by how near death it leaves us. A point is worth its potion price at full health and more as health
    runs out, since the last points are what stands between us and a death (Manual §11 Food and potions)."""
    middle = max(0.5, now + amount / 2)  # the health the change passes through, on average
    return abs(amount) * health() * max(1.0, max_health / middle)


def per_tick(levers: Levers, tick_hz: int) -> float:
    """What one tick of time is worth."""
    return levers.time_worth / 60 / tick_hz


def death(levers: Levers, carried_codes: list[str]) -> float:
    """What dying costs now: a life, and everything carried, since the chest drops where we fall (Manual §11 Death)."""
    return levers.death_weight * (levers.life_worth + sum(item(c) for c in carried_codes))


def gems_of(code: str) -> int:
    """Gems a gem supply adds: ``gem_cache_7`` seven, a plain ``gem`` one."""
    tail = code.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else (1 if code == "gem" else 0)
