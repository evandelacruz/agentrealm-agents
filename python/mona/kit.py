"""The kit: the gear the strategist believes level N needs (docs/v4/SPEC.md **The kit**).

The strategist owns it and may change it as it learns; the goal decider
only reads it. It is kept per character across runs, with the learn steps
already taken for its level.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from agentrealm_adk import supplies

from adk3.realm import value
from adk3.realm.state import State

POTION = "small_potion"
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

    def missing(self, s: State) -> list[str]:
        """What the kit still needs, in get gear's order: each item not carried, then the potions short."""
        carried = [c.code for c in s.carried]
        out = []
        for code in self.items:
            if code in carried:
                carried.remove(code)
            else:
                out.append(code)
        short = self.potions - sum(1 for c in s.carried if supplies.is_potion(c.code))
        out += [POTION] * max(0, short)
        return sorted(out, key=_rank)

    def cost(self, s: State) -> int:
        """Gems the missing pieces cost, at the world's list prices."""
        return int(sum(value.item(code) for code in self.missing(s)))


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
