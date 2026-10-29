"""What counts as progress in Agent Realm (docs/v3/SPEC.md **The measure**). Walking new ground does not."""

from __future__ import annotations

from agentrealm_adk import supplies

from .context import Ctx

GEM_STEP = 25  # each new multiple of this many gems held


def shown(ctx: Ctx) -> list[tuple[str, str]]:
    s, mem = ctx.state, ctx.memory
    out = [(f"level:{n}", f"cleared level {n}") for n in s.levels_cleared]
    out.append((f"map:{s.map_id}", f"entered map {s.map_id}" + (f" (level {s.level})" if s.level else "")))
    for c in s.carried:
        row = supplies.row(c.code)
        if row is not None and row.supply_class in ("weapon", "armor", "tool", "accessory"):
            out.append((f"gear:{c.code}", f"got {c.code}"))
    out += [(f"reached:{k}", f"reached the entrance at {e.x},{e.y}") for k, e in mem.entrances.items() if e.reached]
    out += [(f"clue:{c.speaker or c.source}:{c.text[:40]}", f"learned a clue from a {c.speaker or c.source}")
            for c in mem.clues]
    if s.gems >= GEM_STEP:
        n = s.gems // GEM_STEP * GEM_STEP
        out.append((f"gems:{n}", f"{n} gems"))
    return out
