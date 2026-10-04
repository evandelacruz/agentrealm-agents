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
from .movement import build_paced_walk_queue, step_landing
from .queue import trim_to_horizon

SURVIVAL_EVENTS = frozenset({"Damaged", "Attacked", "Died"})
APPLIED = frozenset({"applied", "applied_no_effect"})


def _target(intent: Intent, at: Pos | None) -> Pos | None:
    """The block a move intent enters from `at`; None for anything else."""
    verb = intent.get("verb")
    if verb == "SetPosition":
        return (intent["x"], intent["y"])
    if verb == "Step" and at is not None:
        return step_landing(at, intent["direction"])
    return None


@dataclass
class InFlight:
    queue_id: str
    intents: list[Intent]
    next_index: int = 0  # next intent index awaiting a result
    anchor: Pos | None = None  # position before the queue was sent
    # The block each move intent enters, by index (None for Wait and the rest).
    targets: list[Pos | None] = field(default_factory=list)


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
        targets: list[Pos | None] = []
        at = anchor
        for intent in intents:
            target = _target(intent, at)
            targets.append(target)
            at = target or at
        self.in_flight = InFlight(queue_id=queue_id, intents=list(intents), anchor=anchor, targets=targets)
        self.invalidated = False

    def build_movement_queue(self, path: list[Pos], w: WorldModel) -> list[Intent]:
        """Step/Wait queue along `path` from `w.pos`, cut after the last Step that fits.

        The first intent runs no earlier than the tick after `w.tick`, so the
        queue opens with the Waits still owed since the last applied move.
        """
        if w.pos is None or not path:
            return []
        since = None
        if self.last_move_tick is not None:
            since = max(1, w.tick + 1 - self.last_move_tick)
        q = build_paced_walk_queue(
            w.pos,
            path,
            movement_speed_milli=w.movement_speed,
            tick_rate_hz=self.tick_rate_hz,
            ticks_since_last_step=since,
        )
        q = trim_to_horizon(q, limit=queue_horizon_intents(tick_rate_hz=self.tick_rate_hz))
        # Waits after the last Step only idle; the next queue opens with what is owed.
        while q and q[-1].get("verb") == "Wait":
            q.pop()
        return q

    def build_from_decision(self, intent: Intent | None, path: list[Pos], w: WorldModel) -> list[Intent] | None:
        if intent is None:
            return None
        if intent.get("verb") == "SetPosition" and w.pos is not None:
            target = (intent["x"], intent["y"])
            if chebyshev(w.pos, target) == 1:
                cells = path if path and path[0] == target else [target]
                return self.build_movement_queue(cells, w) or None
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
            self.in_flight.next_index = idx + 1
            if self._on_applied(intent, self.in_flight.targets[idx], res, w, m):
                return True
        if self.in_flight.next_index >= len(self.in_flight.intents):
            self.in_flight = None
        return False

    def _on_applied(self, intent: Intent, target: Pos | None, result: dict, w: WorldModel, m: Memory) -> bool:
        """Apply one move. True when it dropped the rest of the queue."""
        if target is None:
            return False
        if "tick" in result:
            self.last_move_tick = int(result["tick"])
        if m.path and m.path[0] == target:
            m.path.pop(0)
        if w.view.tiles.get(target) in DOORS:
            # A door moves us; Steps queued behind it would walk from the wrong place.
            if self.in_flight is not None and self.in_flight.next_index < len(self.in_flight.intents):
                self._drop_remainder(m)
                return True
            m.need_position, m.path = True, []
        else:
            w.pos = target
        return False

    def _on_rejected(self, intent: Intent, result: dict, w: WorldModel, m: Memory) -> None:
        # The server discards the rest of the queue on a rejection (GAME_NOTES).
        idx = int(result.get("index", 0))
        if idx == 0 and self.in_flight and self.in_flight.anchor is not None:
            w.pos = self.in_flight.anchor
        target = self.in_flight.targets[idx] if self.in_flight else None
        if target is not None:
            reject_step(m, target)
        m.path, m.need_position = [], True
        if (result.get("rejection") or {}).get("category") == "state":
            m.need_self = True
        self.invalidate(clear_flight=True)

    def invalidate_if_stale(self, w: WorldModel, m: Memory) -> bool:
        """True when remaining queued moves no longer match the world."""
        if not self.active or self.in_flight is None:
            return False
        remaining = self.in_flight.targets[self.in_flight.next_index :]
        if not any(t is not None for t in remaining):
            return False
        if w.pos is None:
            self._drop_remainder(m)
            return True
        cur = w.pos
        occ = w.occupied()
        for target in remaining:
            if target is None:
                continue
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

    def preempt(self, m: Memory) -> None:
        """A reflex fired: drop the in-flight queue so the reflex's intent replaces it."""
        if self.active:
            self._drop_remainder(m)

    def invalidate_from_events(self, events: list[dict], w: WorldModel, m: Memory) -> bool:
        if not self.active:
            return False
        kinds = {ev.get("kind") for ev in events}
        if kinds & SURVIVAL_EVENTS:
            died = "Died" in kinds
            # A death leaves nothing queued to replace.
            self._drop_remainder(m, replace=not died)
            if died:
                m.need_self = True
            return True
        if "BlockChanged" in kinds:
            return self.invalidate_if_stale(w, m)
        return False
