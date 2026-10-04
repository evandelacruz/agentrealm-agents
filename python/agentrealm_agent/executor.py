"""M6 executor: paced multi-intent queues and invalidation.

Re-send only when the in-flight queue goes wrong (rejection, stale path, or
survival events). A rejection or server-side discard drops the remainder;
movement is rebuilt from the current position on the next send.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .brain import Memory, reject_step
from .world import DOORS, Pos, WorldModel, chebyshev

DEFAULT_MOVEMENT_SPEED = 2500  # millimeters per second; 2.5 blocks/s at 10 Hz → 4 ticks per step
QUEUE_HORIZON = 40
SURVIVAL_EVENTS = frozenset({"Damaged", "Attacked", "Died"})


def wait() -> dict:
    return {"verb": "Wait"}


def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def ticks_per_step(tick_rate_hz: int, movement_speed: int) -> int:
    """Move intents spaced by this many ticks (Wait count is one less)."""
    hz = max(1, tick_rate_hz)
    speed = max(1, movement_speed)
    return max(1, (hz * 1000 + speed - 1) // speed)


def paced_set_positions(steps: list[Pos], tick_rate_hz: int, movement_speed: int) -> list[dict]:
    """SetPosition steps with Wait padding for movement_speed."""
    if not steps:
        return []
    gap = ticks_per_step(tick_rate_hz, movement_speed)
    out: list[dict] = []
    for i, p in enumerate(steps):
        out.append(set_position(p))
        if i + 1 < len(steps):
            out.extend(wait() for _ in range(gap - 1))
    return out[:QUEUE_HORIZON]


def movement_targets(intents: list[dict]) -> list[Pos]:
    return [(i["x"], i["y"]) for i in intents if i.get("verb") == "SetPosition"]


@dataclass
class InFlight:
    queue_id: str
    intents: list[dict]
    next_index: int = 0  # next intent index awaiting a result
    anchor: Pos | None = None  # position before the queue was sent


@dataclass
class Executor:
    tick_rate_hz: int = 10
    in_flight: InFlight | None = None
    invalidated: bool = True
    last_rejection: dict | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.in_flight is not None and not self.invalidated

    def invalidate(self, *, clear_flight: bool = True) -> None:
        self.invalidated = True
        if clear_flight:
            self.in_flight = None

    def tick_payload(self, fresh: list[dict] | None) -> list[dict] | None:
        """What to POST: a new queue when invalidated, else poll (None)."""
        if self.active:
            return None
        self.invalidated = False
        return fresh

    def note_sent(self, intents: list[dict] | None, queue_id: str | None, anchor: Pos | None) -> None:
        if intents is None:
            return
        assert queue_id is not None
        self.in_flight = InFlight(queue_id=queue_id, intents=list(intents), anchor=anchor)
        self.invalidated = False
        self.last_rejection = None

    def build_movement_queue(self, path: list[Pos], w: WorldModel) -> list[dict]:
        speed = getattr(w, "movement_speed", DEFAULT_MOVEMENT_SPEED)
        return paced_set_positions(path, self.tick_rate_hz, speed)

    def build_from_decision(self, intent: dict | None, path: list[Pos], w: WorldModel) -> list[dict] | None:
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
        """Apply intent_results for the in-flight queue. True if any rejection."""
        if self.in_flight is None:
            return False
        qid = self.in_flight.queue_id
        ours = sorted(
            (r for r in results if r.get("queue_id") == qid and r.get("index", 0) >= self.in_flight.next_index),
            key=lambda r: r.get("index", 0),
        )
        rejected = False
        for res in ours:
            idx = int(res.get("index", 0))
            if idx >= len(self.in_flight.intents):
                continue
            intent = self.in_flight.intents[idx]
            if res.get("outcome") == "rejected":
                rejected = True
                self.last_rejection = res
                self._on_rejected(intent, res, w, m)
                break
            if res.get("outcome") in ("applied", "applied_no_effect"):
                self._on_applied(intent, w, m)
                self.in_flight.next_index = idx + 1
        if rejected:
            return True
        if self.in_flight and self.in_flight.next_index >= len(self.in_flight.intents):
            self.in_flight = None
        return False

    def _on_applied(self, intent: dict, w: WorldModel, m: Memory) -> None:
        if intent.get("verb") != "SetPosition":
            return
        target = (intent["x"], intent["y"])
        if m.path and m.path[0] == target:
            m.path.pop(0)
        if w.view.tiles.get(target) in DOORS:
            m.need_position, m.path = True, []
        else:
            w.pos = target

    def _on_rejected(self, intent: dict, result: dict, w: WorldModel, m: Memory) -> None:
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
            if intent.get("verb") == "Wait":
                continue
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

    def _drop_remainder(self, m: Memory) -> None:
        """Discard the rest of the in-flight queue and force a replan."""
        m.path = []
        self.invalidate(clear_flight=True)

    def invalidate_from_events(self, events: list[dict], w: WorldModel, m: Memory) -> bool:
        if not self.active:
            return False
        if any(ev.get("kind") in SURVIVAL_EVENTS for ev in events):
            self._drop_remainder(m)
            m.alarm = m.alarm or any(ev.get("kind") in ("Damaged", "Attacked") for ev in events)
            if any(ev.get("kind") == "Died" for ev in events):
                m.need_self = m.need_position = True
            return True
        if any(ev.get("kind") == "BlockChanged" for ev in events):
            return self.invalidate_if_stale(w, m)
        return False
