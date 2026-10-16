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
an ordinary door (``exit_door``, from the room's doors seen since the step
in, so a dark room keeps it; else back to where the boss door landed,
``way_out``): the fight ends, the boss door reopens and the boss keeps the
damage it has taken.
"""

from __future__ import annotations

from .knowledge_base import KnowledgeBase
from .knowledge_maps import door_warp_known, iter_doors
from .memory import BossClock, Memory
from .threat import damage_amount
from .world import DOORS, Pos, WorldModel, chebyshev

# The limit when the level sets none (Manual §11 Bosses and levels).
DEFAULT_LIMIT_SECONDS = 300
# The shortest limit a level sets in Olympuff: what a clock with no door
# step seen assumes (Manual §11 Bosses and levels).
MIN_LIMIT_SECONDS = 90
# The longest limit a level sets in Olympuff: a death later than this from
# the door step is no limit's (Manual §11 Bosses and levels).
MAX_LIMIT_SECONDS = 600
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


def assume(m: Memory, w: WorldModel) -> bool:
    """A boss fight on with no clock (a restart, or the room entered by
    another door than the op's): the clock is assumed to have started now,
    against the shortest limit (``MIN_LIMIT_SECONDS``, or a lower one kept)."""
    if m.boss_clock is not None or w.map_id is None:
        return False
    m.boss_clock = BossClock(w.tick, w.map_id, None, assumed=True)
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
    limit = level_limit_seconds(kb, w.map_levels.get(c.room_map))
    if c.assumed:
        limit = min(limit, MIN_LIMIT_SECONDS)
    limit *= tick_hz
    return max(0, c.door_tick + limit - w.tick)


def note_doors(m: Memory, w: WorldModel) -> None:
    """Keeps the room's doors seen in view, so the way out is known in the dark."""
    c = m.boss_clock
    if c is not None and w.map_id == c.room_map:
        c.doors.update(p for p, b in w.view.tiles.items() if b in DOORS)


def note_death(
    m: Memory,
    w: WorldModel,
    kb: KnowledgeBase | None,
    events: list[dict],
    health_before: int | None,
    map_before: int | None,
    tick_hz: int,
) -> int | None:
    """A death the clock explains is the limit's: a round trip's ``Died``
    while the character stood on the boss room's map (``map_before``, read
    before the round trip: the event's own ``map_id`` is where the chest
    landed, outside the level, Manual §11), inside the fight window (``MAX_LIMIT_SECONDS`` from
    the door step, and no sooner than a limit already kept: a fight outlived
    that one), that its ``Damaged`` hits, every one with its amount, do not
    account for. Its seconds since the door step are kept as the level's
    limit. Any other death leaves the limit as it was. Returns the seconds
    kept, else None."""
    c = m.boss_clock
    died = next((ev for ev in events if ev.get("kind") == "Died"), None)
    if c is None or c.assumed or died is None or kb is None or health_before is None or health_before <= 0:
        return None
    level = w.map_levels.get(c.room_map)
    if not level or map_before != c.room_map:
        return None
    amounts = [damage_amount(ev) for ev in events if ev.get("kind") == "Damaged"]
    if None in amounts or sum(amounts) >= health_before:
        return None
    seconds = round((int(died.get("tick", w.tick)) - c.door_tick) / tick_hz)
    with kb.lock:
        row = kb.levels.get(str(level))
        kept = row.get(LIMIT) if isinstance(row, dict) else None
    if not 0 < seconds <= MAX_LIMIT_SECONDS or (isinstance(kept, int) and seconds < kept):
        return None
    with kb.lock:
        kb.levels.setdefault(str(level), {})[LIMIT] = seconds
    return seconds


def exit_door(m: Memory, w: WorldModel, kb: KnowledgeBase | None) -> Pos | None:
    """The boss room's exit door: a door of the room seen since the step in
    (``note_doors``) or kept in the knowledge base, never a locked one; one
    whose known warp leads back first, the one nearest where the boss door
    landed. None while no door of the room is known."""
    c = m.boss_clock
    if c is None or w.map_id != c.room_map:
        return None
    rows = iter_doors(kb, c.room_map) if kb is not None else []
    locked = {(int(d["x"]), int(d["y"])) for d in rows if d.get("locked")} | {p for p, on in w.view.locked.items() if on}
    known = c.doors | {p for p, b in w.view.tiles.items() if b in DOORS} | {(int(d["x"]), int(d["y"])) for d in rows}
    doors = sorted(known - locked)
    if not doors:
        return None
    back = set()
    if w.passage is not None:
        back = {(int(d["x"]), int(d["y"])) for d in rows if door_warp_known(d) and int(d["to_map_id"]) == w.passage.from_map}
    anchor = c.landing or w.pos or doors[0]
    return min(doors, key=lambda p: (p not in back, chebyshev(p, anchor), p))


def way_out(m: Memory, w: WorldModel, kb: KnowledgeBase | None) -> Pos | None:
    """Where leaving walks: the exit door, else, with none known, back to
    where the boss door landed, beside the door the character came in by.
    None only once there and still no door is known."""
    door = exit_door(m, w, kb)
    if door is not None:
        return door
    c = m.boss_clock
    landing = c.landing if c is not None else None
    return landing if landing is not None and landing != w.pos else None
