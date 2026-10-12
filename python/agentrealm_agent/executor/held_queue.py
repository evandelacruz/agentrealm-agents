"""The queue on the server while it runs: where it may have taken us, and
whether an answer asks for what it already does.

The server runs one intent a tick from the queue it was sent, and a result
reaches us only on a later poll. So while a queue is held, the character may
already stand further along it than the tracked position, and a decision made
from the tracked position may ask again for what the queue is about to do.
Three rules, read by the runner whenever a reflex would replace a held queue
(A63, A116):

- ``possible_positions``: the cells the character may stand on.
- ``held_queue_matches``: an answer that asks for what the held queue already
  does is not sent, so the held queue keeps running.
- ``unconfirmed_send``: what replaces the held queue must be sound from every
  possible position, or the character stops and re-reads position first.
- ``open_step``: under attack a stop only takes free hits, so a walk that is
  not sound from every possible position takes the best Step that is, or,
  with none, the runner hands the decision to Flee's cornered move (A128).

Like ``supply_refusals``, this module reads the world and memory, so the
package does not export it.
"""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..pathing import plan_sets, remaining_walk_cells
from ..reach import in_reach_from
from ..world import DOORS, Pos, WorldModel, chebyshev
from .intents import step
from .movement import DIRECTIONS, step_landing

# What ``unconfirmed_send`` says of a walk whose Steps may land on a blocked cell.
STEP_DOUBT = "the step may land on a blocked cell"


def possible_positions(w: WorldModel, m: Memory) -> list[Pos]:
    """Where the character may stand now: the tracked position, then each
    cell the held queue's Steps with no result yet would have taken it to.

    A Step can run before its result reaches us, so until then any of these
    may be where the next queue starts. The server runs one intent a tick
    from the tick the queue was sent, so only the Steps that elapsed ticks
    could have reached count.
    """
    if w.pos is None:
        return []
    out, pos = [w.pos], w.pos
    if m.pending_intents is None:
        return out
    ran_by = min(len(m.pending_intents), max(0, w.tick - m.queue_sent_tick) + 1)
    for intent in m.pending_intents[m.pending_next_index : ran_by]:
        if intent.get("verb") == "Step":
            pos = step_landing(pos, intent["direction"])
            out.append(pos)
    return out


def held_queue_matches(w: WorldModel, m: Memory, intent: dict | None, submit_queue: list[dict] | None) -> bool:
    """A decision of ``intent`` (or ``submit_queue``) asks for what the held queue already does.

    A walk matches when it goes to the cell the held queue's next Step
    already enters: that queue may already have run the Step without its
    result reaching us, and a fresh Step aimed from the tracked position
    would misaim. Any other intent matches when it is the held queue's
    remaining intents, Waits aside: a held ``Take`` or ``Use`` sent again
    before its result is read only wastes an action.
    """
    if submit_queue is None and intent is not None and intent.get("verb") == "SetPosition":
        ahead = remaining_walk_cells(w, m)
        return bool(ahead) and ahead[0] == (intent["x"], intent["y"])
    wanted = submit_queue if submit_queue is not None else [intent] if intent is not None else []
    if m.pending is not None:
        held = [m.pending]
    elif m.pending_intents is not None:
        held = m.pending_intents[m.pending_next_index :]
    else:
        return False
    acts = [i for i in wanted if i.get("verb") != "Wait"]
    return bool(acts) and acts == [i for i in held if i.get("verb") != "Wait"]


def unconfirmed_send(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    intents: list[dict] | None,
    starts: list[Pos],
) -> str | None:
    """Why ``intents`` cannot go from a position the server has not
    confirmed, or None when they can. ``starts`` are the cells we may stand
    on (``possible_positions``): every Step must land clear and every
    reach-limited intent (a Take, a chest verb, a Use on a target) must reach
    from each of them (``reach.in_reach_from``, A116), or we stop and re-read
    position first."""
    if not intents:
        return None
    if not steps_land_clear(w, m, policy, knowledge, intents, starts):
        return STEP_DOUBT
    if not in_reach_from(intents, starts, w, knowledge):
        return "the target may be out of reach"
    return None


def steps_land_clear(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    intents: list[dict],
    starts: list[Pos],
) -> bool:
    """Every Step in ``intents`` lands on an open cell from each of ``starts``.

    Directions are aimed from the tracked position; with a Step of the old
    queue unconfirmed, the walk may start one or more cells further on and
    land somewhere else. Each such landing must be known walkable, off
    ``avoid_blocks`` hazards and the learned blocked cells.
    """
    directions = [i["direction"] for i in intents if i.get("verb") == "Step"]
    if len(starts) < 2 or not directions:
        return True
    blocked, _, _ = plan_sets(w, m, policy, knowledge)
    for pos in starts[1:]:
        for direction in directions:
            pos = step_landing(pos, direction)
            if not _lands_open(w, blocked, pos):
                return False
    return True


def open_step(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    intents: list[dict],
    starts: list[Pos],
    threats: list[Pos],
) -> list[dict] | None:
    """The best single Step that lands on an open cell from every one of
    ``starts``, for a walk under attack whose Steps do not (A128); None when
    no Step does, or the walk has none.

    The walk's own first Step comes first. Else the open Step whose landing
    from the tracked position keeps furthest from ``threats``, the hostiles
    hitting us, then nearest to where the walk's first Step was going. A
    Step that comes nearer the nearest threat than we stand is never taken.
    """
    directions = [i["direction"] for i in intents if i.get("verb") == "Step"]
    if not directions or not starts:
        return None
    blocked, _, _ = plan_sets(w, m, policy, knowledge)
    here, first = starts[0], directions[0]
    aim = step_landing(here, first)

    def gap(p: Pos) -> int:
        return min((chebyshev(p, t) for t in threats), default=0)

    def open_from_all(direction: str) -> bool:
        return all(_lands_open(w, blocked, step_landing(pos, direction)) for pos in starts)

    def rank(direction: str) -> tuple[int, int, int]:
        land = step_landing(here, direction)
        return (direction != first, -gap(land), chebyshev(land, aim))

    for direction in sorted(DIRECTIONS, key=rank):
        if gap(step_landing(here, direction)) >= gap(here) and open_from_all(direction):
            return [step(direction)]
    return None


def _lands_open(w: WorldModel, blocked: set[Pos], pos: Pos) -> bool:
    """A Step may land on ``pos``: known walkable or a door, and off the blocked cells."""
    return pos not in blocked and (w.walkable(pos) or w.view.tiles.get(pos) in DOORS)
