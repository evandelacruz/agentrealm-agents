"""The milestone ledger: the one measure of progress (docs/v3/SPEC.md **The measure**).

A milestone is a key the game binding names (``level:3``, ``map:12``,
``chunk:4:1,2``) with a label. Each counts once per character, kept across
runs in a JSON file. The run's own milestones give the headline numbers:
minutes since the last one and milestones per hour.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Ledger:
    path: Path | None = None
    seen: dict[str, str] = field(default_factory=dict)  # key -> label, every run
    run: list[tuple[float, str, str]] = field(default_factory=list)  # (time, key, label), this run
    started: float = field(default_factory=time.time)

    @classmethod
    def load(cls, path: Path | None) -> Ledger:
        ledger = cls(path)
        if path is not None and path.exists():
            try:
                ledger.seen = dict(json.loads(path.read_text()).get("seen", {}))
            except (OSError, ValueError):
                pass
        return ledger

    def record(self, key: str, label: str, now: float | None = None) -> bool:
        """Count ``key`` once. True when it is new."""
        if key in self.seen:
            return False
        self.seen[key] = label
        self.run.append((time.time() if now is None else now, key, label))
        return True

    def last_at(self) -> float:
        return self.run[-1][0] if self.run else self.started

    def minutes_since_last(self, now: float | None = None) -> float:
        return ((time.time() if now is None else now) - self.last_at()) / 60.0

    def per_hour(self, now: float | None = None) -> float:
        hours = max(((time.time() if now is None else now) - self.started) / 3600.0, 1 / 60)
        return len(self.run) / hours

    def recent(self, n: int = 8) -> list[str]:
        return [label for _, _, label in self.run[-n:]]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"seen": self.seen}, indent=1, sort_keys=True))
