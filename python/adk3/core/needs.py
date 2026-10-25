"""The needs graph: what the objective requires, down to what can be done now (docs/v3/SPEC.md **The needs graph**).

A need says whether it is met, what it requires (its parts, in the order
to work them), and how it is worked directly once its parts are met (its
skill). The graph is walked from the objective each decision: a met need
is passed over, an unmet one is entered, and the first need with nothing
left under it to work is the **focus**, worked by its skill.

Parts are either **steps** (``ordered``: each must be met before the next
is worked, as a level's: ready, then through the door, then the boss) or
**choices** (any order: the first that can be worked now is).

A need whose skill failed is **blocked** with why, and the walk goes on
past it to its next sibling. The block lifts when what the agent knows
changes (a new fact in memory, a new item, a new map), so a failure is
never retried blindly and never forgotten for good.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar

Ctx = TypeVar("Ctx")


class Status(Enum):
    MET = "met"
    UNMET = "unmet"
    UNKNOWN = "unknown"  # what decides it is not known yet


@dataclass(frozen=True)
class Check:
    status: Status
    why: str = ""

    @staticmethod
    def met(why: str = "") -> Check:
        return Check(Status.MET, why)

    @staticmethod
    def unmet(why: str = "") -> Check:
        return Check(Status.UNMET, why)

    @staticmethod
    def unknown(why: str = "") -> Check:
        return Check(Status.UNKNOWN, why)


class Need(Protocol[Ctx]):
    """One thing the objective requires."""

    @property
    def key(self) -> str:
        """Stable across decisions: ``clear:76:400,37``."""

    @property
    def title(self) -> str:
        """For people and the strategist: ``clear the level behind 400,37``."""

    def check(self, ctx: Ctx) -> Check: ...

    def parts(self, ctx: Ctx) -> list[Need]:
        """What it requires, in the order to work them. Empty for a need worked directly."""

    def skill(self, ctx: Ctx) -> Any:
        """The skill that works it once its parts are met, or None when nothing can."""


def ordered(need: Need) -> bool:
    """Its parts are steps, each met before the next is worked."""
    return bool(getattr(need, "ordered", False))


@dataclass
class Block:
    why: str
    revision: int  # the knowledge revision it was blocked at; any later one lifts it


@dataclass
class Row:
    """One need as the view and the strategist see it."""

    depth: int
    key: str
    title: str
    status: Status
    why: str
    blocked: str = ""
    focus: bool = False


@dataclass
class Graph(Generic[Ctx]):
    root: Need
    blocks: dict[str, Block] = field(default_factory=dict)

    def focus(self, ctx: Ctx, revision: int) -> list[Need]:
        """The path from the objective to the need to work now; empty when nothing can be worked."""
        self.blocks = {k: b for k, b in self.blocks.items() if b.revision >= revision}
        return self._visit(self.root, ctx, set()) or []

    def _visit(self, need: Need, ctx: Ctx, seen: set[str]) -> list[Need] | None:
        if need.key in seen or need.key in self.blocks:
            return None
        if need.check(ctx).status is Status.MET:
            return None
        seen.add(need.key)
        parts = need.parts(ctx)
        for part in parts:
            path = self._visit(part, ctx, seen)
            if path:
                return [need, *path]
            if ordered(need) and part.check(ctx).status is not Status.MET:
                return None  # a step that cannot be worked now holds the steps after it
        if any(p.check(ctx).status is not Status.MET for p in parts):
            return None  # a part is unmet and none of them can be worked now
        return [need] if need.skill(ctx) is not None else None

    def block(self, need: Need, why: str, revision: int) -> None:
        self.blocks[need.key] = Block(why, revision)

    def rows(self, ctx: Ctx, focus: list[Need], max_depth: int = 6) -> list[Row]:
        """The graph as rows, for the view and the strategist: every need down to ``max_depth``."""
        out: list[Row] = []
        on_path = {n.key for n in focus}
        leaf = focus[-1].key if focus else None

        def add(need: Need, depth: int, seen: set[str]) -> None:
            if need.key in seen or depth > max_depth:
                return
            seen = seen | {need.key}
            c = need.check(ctx)
            block = self.blocks.get(need.key)
            out.append(Row(depth, need.key, need.title, c.status, c.why, block.why if block else "",
                           need.key == leaf))
            if c.status is not Status.MET and (need.key in on_path or depth < 2):
                for part in need.parts(ctx):
                    add(part, depth + 1, seen)

        add(self.root, 0, set())
        return out
