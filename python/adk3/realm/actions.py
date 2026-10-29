"""Actions: what an engine can send this tick, each as the intents it becomes (Manual §6).

Every action has a ``key``: while the queue an action sent is still
running, the same action chosen again lets it run instead of sending it
anew. A walk is keyed by its goal; the body also compares its next steps
with the steps still queued, so a walk replanned around a new obstacle
replaces the queue only when its way on changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_adk.executor import build_paced_walk_queue
from agentrealm_adk.world import Pos

WALK_STEPS = 6  # steps sent per walk; the next decision replans from where they leave us


@dataclass(frozen=True)
class Walk:
    path: tuple[Pos, ...]  # cells to enter, in order; the last is the goal or the nearest known way toward it
    why: str

    @property
    def key(self) -> tuple:
        return ("walk", self.path[-1])

    def intents(self, here: Pos, speed: int, tick_hz: int, ticks_since_step: int | None) -> list[dict]:
        return build_paced_walk_queue(here, self.path[:WALK_STEPS], movement_speed_milli=speed, tick_rate_hz=tick_hz,
                                      ticks_since_last_step=ticks_since_step)

    def __str__(self) -> str:
        x, y = self.path[-1]
        return f"walk to {x},{y} ({self.why})"


@dataclass(frozen=True)
class Swing:
    npc_id: int
    why: str

    @property
    def key(self) -> tuple:
        return ("swing", self.npc_id)

    def intents(self) -> list[dict]:
        return [{"verb": "Use", "target": {"kind": "npc", "npc_id": self.npc_id}}]

    def __str__(self) -> str:
        return f"swing at {self.why}"


@dataclass(frozen=True)
class UseOn:
    """Use what is armed (arming ``arm_id`` first) on a block: cut, chop, smash, burn, blast (Manual §11 Breaking blocks)."""

    x: int
    y: int
    arm_id: int | None
    why: str

    @property
    def key(self) -> tuple:
        return ("use", self.x, self.y)

    def intents(self) -> list[dict]:
        out = [{"verb": "Arm", "supply_id": self.arm_id}] if self.arm_id is not None else []
        return out + [{"verb": "Use", "target": {"kind": "block", "x": self.x, "y": self.y}}]

    def __str__(self) -> str:
        return f"{self.why} at {self.x},{self.y}"


@dataclass(frozen=True)
class Drink:
    """A potion is armed, then used on yourself (Manual §11 Food and potions)."""

    supply_id: int
    armed: bool
    code: str

    @property
    def key(self) -> tuple:
        return ("drink", self.supply_id)

    def intents(self) -> list[dict]:
        out = [] if self.armed else [{"verb": "Arm", "supply_id": self.supply_id}]
        return out + [{"verb": "Use", "target": {"kind": "self"}}]

    def __str__(self) -> str:
        return f"drink {self.code}"


@dataclass(frozen=True)
class Equip:
    supply_id: int
    verb: str  # Arm | Wear
    code: str

    @property
    def key(self) -> tuple:
        return ("equip", self.supply_id)

    def intents(self) -> list[dict]:
        return [{"verb": self.verb, "supply_id": self.supply_id}]

    def __str__(self) -> str:
        return f"{self.verb.lower()} {self.code}"


@dataclass(frozen=True)
class Take:
    supply_id: int
    code: str

    @property
    def key(self) -> tuple:
        return ("take", self.supply_id)

    def intents(self) -> list[dict]:
        return [{"verb": "Take", "supply_id": self.supply_id}]

    def __str__(self) -> str:
        return f"take {self.code}"


@dataclass(frozen=True)
class Withdraw:
    """Everything that fits from a ground chest beside us (Manual §6, B117)."""

    chest_id: int

    @property
    def key(self) -> tuple:
        return ("withdraw", self.chest_id)

    def intents(self) -> list[dict]:
        return [{"verb": "WithdrawFromChest", "chest_id": self.chest_id}]

    def __str__(self) -> str:
        return f"empty chest {self.chest_id}"


@dataclass(frozen=True)
class Read:
    x: int
    y: int

    @property
    def key(self) -> tuple:
        return ("read", self.x, self.y)

    def intents(self) -> list[dict]:
        return [{"verb": "Read", "target": {"kind": "block", "x": self.x, "y": self.y}}]

    def __str__(self) -> str:
        return f"read the sign at {self.x},{self.y}"


@dataclass(frozen=True)
class Say:
    npc_id: int
    code: str
    text: str = "hello"

    @property
    def key(self) -> tuple:
        return ("say", self.npc_id)

    def intents(self) -> list[dict]:
        return [{"verb": "Say", "npc_id": self.npc_id, "text": self.text}]

    def __str__(self) -> str:
        return f"say {self.text!r} to {self.code}"


@dataclass(frozen=True)
class Stop:
    """Clear the queue and stand still: nothing better is safe."""

    why: str

    @property
    def key(self) -> tuple:
        return ("stop",)

    def intents(self) -> list[dict]:
        return []

    def __str__(self) -> str:
        return f"stop ({self.why})"


Action = Walk | Swing | UseOn | Drink | Equip | Take | Withdraw | Read | Say | Stop
