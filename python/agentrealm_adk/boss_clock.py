"""The boss fight's clock, counted (A163; Manual §11 Bosses and levels).

A boss fight is on a time limit set per level: 5 minutes when the level
sets none, 90 s to 10 minutes in Olympuff. No read serves the limit or the
time left, so the clock is counted from the tick the step through the boss
door applied (``WorldModel.passage``) and runs while the character stays on
the boss room's map. At the limit health is set to 0 with no roll: a death
the hits taken do not account for. That death's tick less the door's is
the level's limit, kept per level in the knowledge base (``kb.levels``,
``LIMIT``) for the next fight there; until one is learned the default holds.

Before the limit the character can walk out through the room's exit door,
an ordinary door (``exit_door``): the fight ends, the boss door reopens and
the boss keeps the damage it has taken.
"""

from __future__ import annotations

from .knowledge_base import KnowledgeBase
from .knowledge_maps import door_warp_known, iter_doors
from .memory import BossClock, Memory
from .threat import damage_amount
from .world import DOORS, Pos, WorldModel, chebyshev

# The limit when the level sets none (Manual §11 Bosses and levels).
DEFAULT_LIMIT_SECONDS = 300
# The key of a level row's learned boss fight limit, in seconds.
LIMIT = "boss_limit_seconds"


def level_limit_seconds(kb: KnowledgeBase | None, level: int | None) -> int:
    """The level's boss fight limit kept in the knowledge base, else the default."""
    if kb is None or level is None:
        return DEFAULT_LIMIT_SECONDS
    with kb.lock:
        row = kb.levels.get(str(level))
        value = row.get(LIMIT) if isinstance(row, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else DEFAULT_LIMIT_SECONDS


def start(m: Memory, w: WorldModel, door: Pos) -> bool:
    """Starts the clock on a step through ``door`` read since the last one
    started, while the character is still on the map it led to."""
    p = w.passage
    if p is None or p.from_pos != door or w.map_id != p.to_map:
        return False
    if m.boss_clock is not None and m.boss_clock.door_tick >= p.tick:
        return False
    m.boss_clock = BossClock(p.tick, p.to_map, p.to_pos)
    return True


def running(m: Memory, w: WorldModel) -> bool:
    """The clock runs: alive, and still on the boss room's map."""
    c = m.boss_clock
    return c is not None and w.alive and w.map_id == c.room_map


def ticks_left(m: Memory, w: WorldModel, kb: KnowledgeBase | None, tick_hz: int) -> int | None:
    """Ticks left before the limit, or None while no clock runs."""
    c = m.boss_clock
    if c is None or not running(m, w):
        return None
    limit = level_limit_seconds(kb, w.map_levels.get(c.room_map)) * tick_hz
    return max(0, c.door_tick + limit - w.tick)


def note_death(
    m: Memory,
    w: WorldModel,
    kb: KnowledgeBase | None,
    events: list[dict],
    health_before: int | None,
    tick_hz: int,
) -> int | None:
    """A round trip's ``Died`` in the boss room that its ``Damaged`` hits do
    not account for is the limit's: its seconds since the door step are
    kept as the level's limit. Returns the seconds kept, else None."""
    c = m.boss_clock
    died = next((ev for ev in events if ev.get("kind") == "Died"), None)
    if c is None or died is None or kb is None or health_before is None or health_before <= 0:
        return None
    level = w.map_levels.get(c.room_map)
    if not level:
        return None
    hits = sum(damage_amount(ev) or 0 for ev in events if ev.get("kind") == "Damaged")
    if hits >= health_before:
        return None
    tick = int(died.get("tick", w.tick))
    seconds = round((tick - c.door_tick) / tick_hz)
    if seconds <= 0:
        return None
    with kb.lock:
        kb.levels.setdefault(str(level), {})[LIMIT] = seconds
    return seconds


def exit_door(m: Memory, w: WorldModel, kb: KnowledgeBase | None) -> Pos | None:
    """The boss room's exit door: a door seen on the room's map, one whose
    known warp leads back first, the one nearest where the boss door landed."""
    c = m.boss_clock
    if c is None or w.map_id != c.room_map:
        return None
    doors = [p for p, b in w.view.tiles.items() if b in DOORS and not w.view.locked.get(p)]
    if not doors:
        return None
    back = set()
    if kb is not None and w.passage is not None:
        for d in iter_doors(kb, c.room_map):
            if door_warp_known(d) and int(d["to_map_id"]) == w.passage.from_map:
                back.add((int(d["x"]), int(d["y"])))
    anchor = c.landing or w.pos or doors[0]
    return min(doors, key=lambda p: (p not in back, chebyshev(p, anchor), p))
