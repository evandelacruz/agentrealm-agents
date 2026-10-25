"""What counts as progress in Agent Realm, and what makes a plan worth asking again (docs/v3/SPEC.md **The measure**)."""

from __future__ import annotations

from .state import State

GEM_STEP = 25  # gems: each new multiple held is a milestone


def shown(s: State) -> list[tuple[str, str]]:
    out = [(f"level:{n}", f"cleared level {n}") for n in s.levels_cleared]
    out.append((f"map:{s.map_id}", f"entered map {s.map_id}" + (f" (level {s.level})" if s.level else "")))
    out += [(f"item:{c}", f"got {c}") for c in s.codes()]
    if s.gems >= GEM_STEP:
        n = s.gems // GEM_STEP * GEM_STEP
        out.append((f"gems:{n}", f"{n} gems"))
    return out


def surprises(before: State, s: State, events: list[dict]) -> list[str]:
    out = []
    if any(ev.get("kind") == "Died" for ev in events) or s.lives < before.lives:
        out.append("we died")
    if s.map_id != before.map_id:
        out.append(f"now on map {s.map_id}" + (f" (level {s.level})" if s.level else ""))
    new_levels = set(s.levels_cleared) - set(before.levels_cleared)
    if new_levels:
        out.append(f"cleared level {', '.join(map(str, sorted(new_levels)))}")
    new_items = s.codes() - before.codes()
    if new_items:
        out.append(f"got {', '.join(sorted(new_items))}")
    return out
