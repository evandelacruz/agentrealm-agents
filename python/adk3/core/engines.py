"""Engines: real-time play, one per kind of situation (docs/v3/SPEC.md **Engines**).

Every tick the first engine whose situation holds acts: it turns the
moment, the skill's order and the strategist's tactics into one action,
with no model call. Engines are listed most urgent first, so a threat
outranks the skill's own engine.
"""

from __future__ import annotations

from typing import Any, Protocol


class Engine(Protocol):
    name: str

    def applies(self, ctx: Any, order: Any) -> bool:
        """This engine's situation holds now."""

    def act(self, ctx: Any, order: Any) -> Any:
        """The action for this tick, or None to send nothing."""


def pick(engines: list[Engine], ctx: Any, order: Any) -> Engine | None:
    return next((e for e in engines if e.applies(ctx, order)), None)
