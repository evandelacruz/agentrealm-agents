"""Run metrics from a character trace (A41) and compare runs (A42).

Parses JSONL written by ``Runner`` and returns counts and timings for
evaluation (M12): levels cleared, deaths, kills, gems, and time per level.

The trace is append-only across runs. Metrics cover the last run only: each
run starts with a ``world`` record, and everything before the last one is
ignored.

``compare_run_metrics`` subtracts a baseline from a candidate so a regression
shows up as a number (M12).
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
    bad_lines: int = 0  # unparseable trace lines, e.g. one cut short by a kill

    def to_dict(self) -> dict[str, Any]:
        return {
            "deaths": self.deaths,
            "kills": self.kills,
            "gems": self.gems,
            "levels_cleared": self.levels_cleared,
            "time_per_level": {str(k): v for k, v in sorted(self.time_per_level.items())},
            "bad_lines": self.bad_lines,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunMetrics:
        tpl = raw.get("time_per_level") or {}
        time_per_level = {int(k): float(v) for k, v in tpl.items()}
        gems = raw.get("gems")
        return cls(
            deaths=int(raw.get("deaths", 0)),
            kills=int(raw.get("kills", 0)),
            gems=None if gems is None else int(gems),
            levels_cleared=int(raw.get("levels_cleared", 0)),
            time_per_level=time_per_level,
            bad_lines=int(raw.get("bad_lines", 0)),
        )


class LevelTimer:
    """Wall-clock and tick span since the character entered the level it is in.

    A level is a set of maps behind an entrance door on the overworld
    (docs/GAME_NOTES.md Levels and bosses), so the span starts when the
    character leaves the overworld (the town's map) and runs across every map
    inside the level. Returning to the overworld or clearing a level ends it.
    A run that starts inside a level has no span for that level. With the
    overworld unknown, the span starts at the first map seen and after each
    clear.
    """

    def __init__(self, overworld: int | None = None) -> None:
        self.overworld = overworld
        self._map_id: int | None = None
        self._entered_t: float | None = None
        self._entered_tick: int | None = None

    def note_map(self, map_id: int | None, tick: int, now: float) -> None:
        if map_id is None or map_id == self._map_id:
            return
        prev, self._map_id = self._map_id, map_id
        if map_id == self.overworld:
            self.forget_span()
        elif prev == self.overworld:
            self._entered_t = now
            self._entered_tick = tick

    def duration(self, now: float, tick: int) -> tuple[float | None, int | None]:
        if self._entered_t is None or self._entered_tick is None:
            return None, None
        return now - self._entered_t, tick - self._entered_tick

    def forget_span(self) -> None:
        self._entered_t = None
        self._entered_tick = None

    def cleared(self) -> None:
        """The level was cleared and the character moved outside it.

        The map is forgotten too, so with the overworld known a stale read of
        the level's map in the same tick does not start a new span.
        """
        self.forget_span()
        self._map_id = None


def iter_trace(path: Path, bad: list[int] | None = None) -> Iterable[dict[str, Any]]:
    """Yields each record; a line that does not parse is skipped and counted in ``bad``."""
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                rec = None
            if not isinstance(rec, dict):
                if bad is not None:
                    bad[0] += 1
                continue
            yield rec


def last_run(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records from the last run's ``world`` record on (all of them if none)."""
    out: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("call") == "world" and "world" in rec:
            out = []
        out.append(rec)
    return out


def compute_metrics(records: Iterable[dict[str, Any]]) -> RunMetrics:
    out = RunMetrics()
    cleared_levels: set[int] = set()
    for rec in last_run(records):
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
    bad = [0]
    out = compute_metrics(iter_trace(path, bad))
    out.bad_lines = bad[0]
    return out


def _parse_metrics_json(text: str) -> RunMetrics:
    return RunMetrics.from_dict(json.loads(text))


def _parse_metrics_cli_line(text: str) -> RunMetrics:
    """One line from ``metrics`` stdout: ``name: {json}``."""
    line = text.strip()
    if not line:
        raise ValueError("empty metrics line")
    if ": " not in line:
        return _parse_metrics_json(line)
    _, payload = line.split(": ", 1)
    return _parse_metrics_json(payload)


def load_metrics_source(path: Path) -> RunMetrics:
    """Metrics from a trace (``.jsonl``), snapshot (``.json``), or CLI capture."""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return metrics_from_trace(path)
    if suffix == ".json":
        return _parse_metrics_json(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line:
            return _parse_metrics_cli_line(line)
    raise ValueError(f"no metrics in {path}")


def compare_run_metrics(baseline: RunMetrics, candidate: RunMetrics) -> dict[str, Any]:
    """Numeric deltas (candidate minus baseline) for each metric field."""
    diff: dict[str, Any] = {
        "deaths": candidate.deaths - baseline.deaths,
        "kills": candidate.kills - baseline.kills,
        "levels_cleared": candidate.levels_cleared - baseline.levels_cleared,
        "bad_lines": candidate.bad_lines - baseline.bad_lines,
    }
    if baseline.gems is not None and candidate.gems is not None:
        diff["gems"] = candidate.gems - baseline.gems
    else:
        diff["gems"] = None
    levels = set(baseline.time_per_level) | set(candidate.time_per_level)
    diff["time_per_level"] = {
        str(level): (candidate.time_per_level.get(level, 0.0) - baseline.time_per_level.get(level, 0.0))
        for level in sorted(levels)
    }
    return diff


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
        level_timer.cleared()
    return extra
