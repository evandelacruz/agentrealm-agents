"""Run metrics from a character trace (A41).

Parses JSONL written by ``Runner`` and returns counts and timings for
evaluation (M12): levels cleared, deaths, kills, gems, and time per level.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class RunMetrics:
    deaths: int = 0
    kills: int = 0
    gems: int | None = None
    levels_cleared: int = 0
    time_per_level: dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deaths": self.deaths,
            "kills": self.kills,
            "gems": self.gems,
            "levels_cleared": self.levels_cleared,
            "time_per_level": {str(k): v for k, v in sorted(self.time_per_level.items())},
        }


class LevelTimer:
    """Wall-clock and tick span since the character last changed maps."""

    def __init__(self) -> None:
        self._map_id: int | None = None
        self._entered_t: float | None = None
        self._entered_tick: int | None = None

    def note_map(self, map_id: int | None, tick: int, now: float) -> None:
        if map_id is None or map_id == self._map_id:
            return
        self._map_id = map_id
        self._entered_t = now
        self._entered_tick = tick

    def duration(self, now: float, tick: int) -> tuple[float | None, int | None]:
        if self._entered_t is None or self._entered_tick is None:
            return None, None
        return now - self._entered_t, tick - self._entered_tick

    def forget_span(self) -> None:
        self._entered_t = None
        self._entered_tick = None


def iter_trace(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def compute_metrics(records: Iterable[dict[str, Any]]) -> RunMetrics:
    out = RunMetrics()
    cleared_levels: set[int] = set()
    for rec in records:
        for ev in rec.get("events") or []:
            kind = ev.get("kind")
            if kind == "Died":
                out.deaths += 1
            elif kind == "NPCDied":
                out.kills += 1
        if "gems" in rec:
            out.gems = int(rec["gems"])
        ceremony = rec.get("level_clear_ceremony")
        if isinstance(ceremony, dict) and "level_number" in ceremony:
            level = int(ceremony["level_number"])
            cleared_levels.add(level)
            if "level_duration_s" in rec:
                out.time_per_level[level] = float(rec["level_duration_s"])
    out.levels_cleared = len(cleared_levels)
    return out


def metrics_from_trace(path: Path) -> RunMetrics:
    return compute_metrics(iter_trace(path))


def tick_trace_extras(
    *,
    tick_response: dict[str, Any],
    gems: int | None,
    level_timer: LevelTimer,
    tick: int,
    now: float,
) -> dict[str, Any]:
    """Fields to merge into a tick trace line for later metric parsing."""
    extra: dict[str, Any] = {}
    if gems is not None:
        extra["gems"] = gems
    ceremony = tick_response.get("level_clear_ceremony")
    if isinstance(ceremony, dict):
        extra["level_clear_ceremony"] = ceremony
        duration_s, duration_ticks = level_timer.duration(now, tick)
        if duration_s is not None:
            extra["level_duration_s"] = duration_s
        if duration_ticks is not None:
            extra["level_duration_ticks"] = duration_ticks
        level_timer.forget_span()
    return extra
