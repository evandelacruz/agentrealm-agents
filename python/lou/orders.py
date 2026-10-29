"""Orders: what a skill asks of the engines this decision. The engines decide how, in real time."""

from __future__ import annotations

from dataclasses import dataclass

from adk3.realm.actions import Action
from agentrealm_adk.world import Pos


@dataclass(frozen=True)
class GoTo:
    """Walk to a cell, on any map; through a door onto the map behind it when ``door``."""

    map_id: int
    cell: Pos
    why: str
    door: bool = False


@dataclass(frozen=True)
class Explore:
    """Walk to unseen ground, toward ``toward`` when given."""

    why: str
    toward: Pos | None = None


@dataclass(frozen=True)
class Do:
    """Stand within ``reach`` of ``at`` and do ``action`` (take, read, talk, use, equip, empty a chest)."""

    action: Action
    at: Pos
    reach: int = 1


@dataclass(frozen=True)
class Work:
    """Break blocks for what they drop, best first: earning gems by field work (Manual §16)."""

    cells: tuple[Pos, ...]
    why: str


@dataclass(frozen=True)
class Fight:
    """Fight this NPC to the end, on the combat model; a boss on its clock."""

    npc_id: int
    why: str
    boss: bool = False


Order = GoTo | Explore | Do | Work | Fight
