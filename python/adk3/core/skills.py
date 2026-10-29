"""Skills: the methods an approach is carried out by (docs/v3/SPEC.md **The executive and the methods**).

A skill is a small procedure with settings: earn gems in a field, buy an
item at a shop, travel to a place. Each decision it says what it wants
now, its **order** (go to this place, work these blocks, take this item),
which the reactions carry out in real time; why it cannot go on, if it
cannot; and a progress number that rises as it nears done, so a skill that
stops progressing can be called failed.
"""

from __future__ import annotations

from typing import Any, Protocol


class Skill(Protocol):
    name: str

    def order(self, ctx: Any) -> Any:
        """What the skill wants of the reactions now, or None when it has nothing to ask."""

    def failed(self, ctx: Any) -> str | None:
        """Why the skill cannot go on, or None."""

    def progress(self, ctx: Any) -> float: ...
