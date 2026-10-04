"""M6 executor: paced multi-intent queues and invalidation.

Re-send only when the in-flight queue goes wrong (rejection, stale path,
survival events, or an outcome we do not know). A rejection discards the
remainder server-side; any other drop replaces the held queue, so the server
never keeps running moves the client has given up on. Movement is rebuilt from
the current position on the next send.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..brain import Memory, reject_step
from ..client import Intent
from ..world import DOORS, Pos, WorldModel, chebyshev
from .constants import DEFAULT_TICK_RATE_HZ, queue_horizon_intents
from .intents import wait
from .movement import ticks_per_step
from .queue import trim_to_horizon

DEFAULT_MOVEMENT_SPEED = 2500  # millimeters per second; 2.5 blocks/s at 10 Hz → 4 ticks per step
SURVIVAL_EVENTS = frozenset({"Damaged", "Attacked", "Died"})
APPLIED = frozenset({"applied", "applied_no_effect"})


def set_position(p: Pos) -> Intent:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def _gap(tick_rate_hz: int, movement_speed: int) -> int:
    """Move intents spaced by this many ticks (Wait count is one less)."""
    return ticks_per_step(
        tick_rate_hz=max(1, tick_rate_hz), movement_speed_milli=max(1, movement_speed)
    )


def paced_set_positions(
    steps: list[Pos],
    tick_rate_hz: int,
    movement_speed: int,
    *,
    lead_waits: int = 0,
) -> list[Intent]:
    """SetPosition steps, each followed by its Wait run, cut on step boundaries.

    Every step carries its trailing waits, so a queue that runs to the end leaves
    the move accumulator full for the next send's first step. `lead_waits` pads
    the front when the last move landed too recently.
    """
    if not steps:
        return []
    gap = _gap(tick_rate_hz, movement_speed)
    limit = queue_horizon_intents(tick_rate_hz=max(1, tick_rate_hz))
    lead = min(max(0, lead_waits), gap - 1)
    out: list[Intent] = [wait() for _ in range(lead)]
    for p in steps:
        unit = [set_position(p)] + [wait() for _ in range(gap - 1)]
        if len(out) + len(unit) > limit and any(i.get("verb") == "SetPosition" for i in out):
            break
        out.extend(unit)
    return trim_to_horizon(out, limit=limit)


def movement_targets(intents: list[Intent]) -> list[Pos]:
    return [(i["x"], i["y"]) for i in intents if i.get("verb") == "SetPosition"]


@dataclass
class InFlight:
    queue_id: str
    intents: list[Intent]
    next_index: int = 0  # next intent index awaiting a result
    anchor: Pos | None = None  # position before the queue was sent


@dataclass
class Executor:
    tick_rate_hz: int = DEFAULT_TICK_RATE_HZ
    in_flight: InFlight | None = None
    invalidated: bool = True
    # The server may still hold intents we dropped; the next send must replace them.
    replace_held: bool = False
    last_move_tick: int | None = None
    last_rejection: dict | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.in_flight is not None and not self.invalidated

    def invalidate(self, *, clear_flight: bool = True) -> None:
        self.invalidated = True
        if clear_flight:
            self.in_flight = None

    def tick_payload(self, fresh: list[Intent] | None) -> list[Intent] | None:
        """What to POST: a new queue when invalidated, else poll (None).

        After a drop with nothing new to do, a lone Wait replaces the held queue.
        """
        if self.active:
            return None
        self.invalidated = False
        if fresh is None and self.replace_held:
            return [wait()]
        return fresh

    def note_sent(
        self,
        intents: list[Intent] | None,
        queue_id: str | None,
        anchor: Pos | None,
        m: Memory,
    ) -> None:
        if intents is None:
            return
        self.replace_held = False
        self.last_rejection = None
        if queue_id is None:
            # Results can't be matched to this queue. Read where we are and replan.
            self.in_flight = None
            self.invalidated = True
            m.path, m.need_position = [], True
            return
        self.in_flight = InFlight(queue_id=queue_id, intents=list(intents), anchor=anchor)
        self.invalidated = False

    def build_movement_queue(self, path: list[Pos], w: WorldModel) -> list[Intent]:
        speed = getattr(w, "movement_speed", DEFAULT_MOVEMENT_SPEED)
        lead = 0
        if self.last_move_tick is not None:
            gap = _gap(self.tick_rate_hz, speed)
            # The first intent runs no earlier than the tick after w.tick.
            lead = self.last_move_tick + gap - (w.tick + 1)
        return paced_set_positions(path, self.tick_rate_hz, speed, lead_waits=lead)

    def build_from_decision(self, intent: Intent | None, path: list[Pos], w: WorldModel) -> list[Intent] | None:
        if intent is None:
            return None
        if intent.get("verb") == "SetPosition" and len(path) > 1:
            return self.build_movement_queue(path, w)
        return [intent]

    def ingest_results(
        self,
        results: list[dict],
        w: WorldModel,
        m: Memory,
    ) -> bool:
        """Apply intent_results for the in-flight queue. True if it was dropped."""
        if self.in_flight is None:
            return False
        qid = self.in_flight.queue_id
        ours = sorted(
            (r for r in results if r.get("queue_id") == qid and r.get("index", 0) >= self.in_flight.next_index),
            key=lambda r: r.get("index", 0),
        )
        for res in ours:
            idx = int(res.get("index", 0))
            if idx >= len(self.in_flight.intents):
                continue
            intent = self.in_flight.intents[idx]
            outcome = res.get("outcome")
            if outcome == "rejected":
                self.last_rejection = res
                self._on_rejected(intent, res, w, m)
                return True
            if outcome not in APPLIED:
                # An outcome we don't know: we can't tell where we are or what
                # the server still holds.
                self.last_rejection = res
                self._drop_remainder(m)
                return True
            self._on_applied(intent, res, w, m)
            self.in_flight.next_index = idx + 1
        if self.in_flight.next_index >= len(self.in_flight.intents):
            self.in_flight = None
        return False

    def _on_applied(self, intent: Intent, result: dict, w: WorldModel, m: Memory) -> None:
        if intent.get("verb") != "SetPosition":
            return
        if "tick" in result:
            self.last_move_tick = int(result["tick"])
        target = (intent["x"], intent["y"])
        if m.path and m.path[0] == target:
            m.path.pop(0)
        if w.view.tiles.get(target) in DOORS:
            m.need_position, m.path = True, []
        else:
            w.pos = target

    def _on_rejected(self, intent: Intent, result: dict, w: WorldModel, m: Memory) -> None:
        # The server discards the rest of the queue on a rejection (GAME_NOTES).
        idx = int(result.get("index", 0))
        if idx == 0 and self.in_flight and self.in_flight.anchor is not None:
            w.pos = self.in_flight.anchor
        if intent.get("verb") == "SetPosition":
            reject_step(m, (intent["x"], intent["y"]))
        m.path, m.need_position = [], True
        if (result.get("rejection") or {}).get("category") == "state":
            m.need_self = True
        self.invalidate(clear_flight=True)

    def invalidate_if_stale(self, w: WorldModel, m: Memory) -> bool:
        """True when remaining queued moves no longer match the world."""
        if not self.active or self.in_flight is None:
            return False
        remaining = self.in_flight.intents[self.in_flight.next_index :]
        if not remaining:
            return False
        if w.pos is None:
            self._drop_remainder(m)
            return True
        cur = w.pos
        occ = w.occupied()
        for intent in remaining:
            if intent.get("verb") != "SetPosition":
                continue
            target = (intent["x"], intent["y"])
            if chebyshev(cur, target) > w.movement:
                self._drop_remainder(m)
                return True
            block = w.view.tiles.get(target)
            if block in DOORS:
                cur = target
                continue
            if target in occ or not w.view.walkable(target):
                self._drop_remainder(m)
                return True
            cur = target
        return False

    def _drop_remainder(self, m: Memory, *, replace: bool = True) -> None:
        """Give up on the in-flight queue and force a position read and replan.

        Results for the dropped queue are no longer read, so the local position
        can't be trusted. Unless the queue is already gone server-side, the next
        send replaces it.
        """
        m.path, m.need_position = [], True
        self.replace_held = self.replace_held or replace
        self.invalidate(clear_flight=True)

    def invalidate_from_events(self, events: list[dict], w: WorldModel, m: Memory) -> bool:
        if not self.active:
            return False
        kinds = {ev.get("kind") for ev in events}
        if kinds & SURVIVAL_EVENTS:
            died = "Died" in kinds
            # A death leaves nothing queued to replace.
            self._drop_remainder(m, replace=not died)
            m.alarm = m.alarm or bool(kinds & {"Damaged", "Attacked"})
            if died:
                m.need_self = True
            return True
        if "BlockChanged" in kinds:
            return self.invalidate_if_stale(w, m)
        return False
