"""Kits: what she must carry to set off for a destination (docs/v4/SPEC.md **Kits**).

Every destination has one, empty or not, checked once when she sets off and
never on the way or inside: what she spends out there does not turn her
back. Level N's kit is the strategist's (``Kit``), kept per character across
runs with the learn steps taken for its level. The outer band's is fixed
(``OUTER``); every other destination's is empty.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from agentrealm_adk import supplies

from adk3.realm import value
from adk3.realm.state import State

POTION = "small_potion"
OUTER = (["bronze_sword", "bronze_mail"], 1)  # the outer band's kit: a basic sword, armor and a potion
ORDER = ("weapon", "armor", "tool", "consumable")  # get gear takes the missing pieces in this order


@dataclass
class Kit:
    level: int
    entrance: str | None = None  # the entrance key ("map:x,y") the strategist judges is level N's
    items: list[str] = field(default_factory=list)  # gear to hold, arm or wear: weapon, armor, key, bomb, raft
    potions: int = 0  # small potions to carry
    trusted: bool = False
    why: str = ""
    learned: list[str] = field(default_factory=list)  # learn steps taken for this level
    set_off: bool = False  # she met this kit and set off for the level: not checked again until a death or a new kit

    def missing(self, s: State) -> list[str]:
        """What the kit still needs, in get gear's order: each item not carried, then the potions short."""
        return missing(s, self.items, self.potions)

    def same_as(self, other: Kit | None) -> bool:
        """The same gear for the same level and door: a revision that changes nothing keeps her on her way."""
        return other is not None and (self.level, self.entrance, sorted(self.items), self.potions) == \
            (other.level, other.entrance, sorted(other.items), other.potions)

    def cost(self, s: State) -> int:
        """Gems the missing pieces cost, at the world's list prices."""
        return int(sum(value.item(code) for code in self.missing(s)))


def missing(s: State, items: list[str], potions: int) -> list[str]:
    """What a kit still needs, in get gear's order: each item not carried, then the potions short."""
    carried = [c.code for c in s.carried]
    out = []
    for code in items:
        if code in carried:
            carried.remove(code)
        else:
            out.append(code)
    short = potions - sum(1 for c in s.carried if supplies.is_potion(c.code))
    out += [POTION] * max(0, short)
    return sorted(out, key=_rank)


def _rank(code: str) -> int:
    r = supplies.row(code)
    kind = r.supply_class if r is not None else "tool"
    return ORDER.index(kind) if kind in ORDER else len(ORDER)


class KitStore:
    def __init__(self, path: Path | None):
        self.path = path

    def load(self) -> Kit | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        names = {f.name for f in fields(Kit)}
        return Kit(**{k: v for k, v in raw.items() if k in names})

    def save(self, kit: Kit) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(kit), indent=1))
