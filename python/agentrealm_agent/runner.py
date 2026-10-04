"""The loop: one call per window, paced by the wall clock."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass

from .brain import Decision, Memory, choose_call, decide
from .client import ApiError, Client
from .config import CharacterConfig
from .executor import Executor
from .world import WorldModel, terrain_cells

# Land a little after a window opens, so a clock skew of a few ms does not put
# two calls in one window.
WINDOW_MARGIN = 0.05


@dataclass
class Pacer:
    """Paces calls to the front tier's rate-limit windows.

    The limiter buckets wall-clock time as epoch / tick interval
    (internal/api/ratelimit.go), so the agent does the same.
    """

    window: float

    def wait_next_window(self, not_before: float = 0.0) -> None:
        now = time.time()
        next_open = (int(now / self.window) + 1) * self.window + WINDOW_MARGIN
        time.sleep(max(next_open, not_before) - now)


class Runner:
    def __init__(self, cfg: CharacterConfig, client: Client, character_id: int, stop: threading.Event, out=print):
        self.cfg = cfg
        self.client = client
        self.cid = character_id
        self.stop = stop
        self.out = out
        self.world = WorldModel(character_id)
        self.mem = Memory()
        seed = cfg.policy.seed if cfg.policy.seed is not None else character_id
        self.rng = random.Random(seed)
        self.pacer = Pacer(1.0)
        self.executor = Executor()
        self.tick_rate_hz = 10
        cfg.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace = open(cfg.trace_path, "a", buffering=1)

    def log(self, call: str, detail: str, record: dict) -> None:
        w = self.world
        pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos else "?"
        self.out(f"[{self.cfg.name}] t={w.tick} @{pos} {call:<8} {detail}")
        self.trace.write(json.dumps({"t": time.time(), "tick": w.tick, "pos": w.pos, "map": w.map_id, "call": call, **record}) + "\n")

    def run(self) -> None:
        world = self.read_world()
        if world is None:
            self.trace.close()
            return
        hz = max(1, int(world.get("tick_rate_hz", 1)))
        self.tick_rate_hz = hz
        self.executor.tick_rate_hz = hz
        self.pacer = Pacer(1.0 / hz)
        self.log("world", f"{world.get('code')} {world.get('status')} {hz}Hz", {"world": world})
        not_before = 0.0
        try:
            while not self.stop.is_set():
                self.pacer.wait_next_window(not_before)
                not_before = 0.0
                call = choose_call(self.world, self.mem, self.cfg.policy)
                try:
                    not_before = self.step(call)
                except ApiError as e:
                    not_before = self.on_error(call, e)
        finally:
            self.trace.close()

    def read_world(self) -> dict | None:
        """The world read that sets the pace, retried like any other call."""
        while not self.stop.is_set():
            try:
                return self.client.world(self.cid)
            except ApiError as e:
                self.stop.wait(max(0.0, self.on_error("world", e) - time.time()) or 1.0)
        return None

    def step(self, call: str) -> float:
        """Spends this window's call. Returns the earliest time for the next one."""
        w, m, c = self.world, self.mem, self.client
        m.windows_since_self += 1
        if call == "self":
            s = c.self_(self.cid)
            w.apply_self(s)
            m.need_self, m.windows_since_self = False, 0
            self.log(call, f"lives={w.lives} alive={w.alive} placed={s.get('placed')} perception={w.perception}", {"self": s})
        elif call == "position":
            p = c.position(self.cid)
            w.apply_position(p)
            m.need_position, m.path, m.undo = False, [], None
            self.log(call, "", {"position": p})
        elif call == "terrain":
            t = c.terrain(self.cid, w.map_id, *w.perception_rect())
            w.apply_terrain(t)
            w.tick = max(w.tick, int(t.get("tick", 0)))
            self.executor.invalidate_if_stale(w, m)
            n = len(terrain_cells(t))
            self.log(call, f"{n} cells, {len(w.view.tiles)} known", {"cells": n})
        elif call == "entities":
            e = c.entities(self.cid, w.map_id, *w.perception_rect())
            w.apply_entities(e)
            w.tick = max(w.tick, int(e.get("tick", 0)))
            self.executor.invalidate_if_stale(w, m)
            m.alarm = False
            seen = ", ".join(f"{x.kind}:{x.id}@{x.pos[0]},{x.pos[1]}" for x in w.entities) or "nobody"
            self.log(call, seen, {"entities": e})
        else:
            return self.tick()
        return 0.0

    def tick(self) -> float:
        w, m, ex = self.world, self.mem, self.executor
        if ex.active:
            d = Decision(None, "queue in flight")
        else:
            d = decide(w, m, self.cfg.policy, self.rng)
        fresh = ex.build_from_decision(d.intent, m.path, w)
        payload = ex.tick_payload(fresh)
        r = self.client.tick(self.cid, payload)
        w.tick = int(r.get("tick", w.tick))
        rejected = ex.ingest_results(r.get("intent_results") or [], w, m)
        if payload is not None:
            ex.note_sent(payload, r.get("queue_id"), w.pos)
        events = w.apply_events(r.get("events_by_tick") or [])
        w.apply_observation(r.get("observation"))
        ex.invalidate_from_events(events, w, m)
        ex.invalidate_if_stale(w, m)
        self.on_events(events)
        submitted = payload
        detail = f"{_fmt_intents(submitted)} ({d.reason})"
        last = self._last_result(r.get("intent_results") or [])
        if last is not None:
            detail += f" | last {_fmt_result(last)}"
        if events:
            detail += " | " + ", ".join(_fmt_event(e) for e in events)
        if r.get("events_dropped"):
            detail += f" | dropped {r['events_dropped']}"
        self.log(
            "tick",
            detail,
            {
                "intents": submitted,
                "reason": d.reason,
                "result": last,
                "events": events,
                "dropped": r.get("events_dropped", 0),
                "queue_invalidated": rejected or ex.invalidated,
            },
        )
        return time.time() + int(r.get("window_remaining_ms", 0)) / 1000.0 + WINDOW_MARGIN

    def _last_result(self, results: list[dict]) -> dict | None:
        ex = self.executor
        if ex.in_flight is None:
            if ex.last_rejection is not None:
                return ex.last_rejection
            return None
        qid = ex.in_flight.queue_id
        ours = [r for r in results if r.get("queue_id") == qid]
        return max(ours, key=lambda r: r.get("index", 0)) if ours else ex.last_rejection

    def on_events(self, events: list[dict]) -> None:
        w, m = self.world, self.mem
        for ev in events:
            kind = ev.get("kind")
            # These happen to the queue owner and carry no subject_id (docs/API.md, Events).
            if kind in ("Damaged", "Attacked"):
                m.alarm = True
            if kind == "Died":
                m.need_self = m.need_position = True
                m.path = []

    def on_error(self, call: str, e: ApiError) -> float:
        self.log(call, f"error {e}", {"error": {"status": e.status, "code": e.code}})
        if e.status in (401, 403):
            raise e
        if e.paused or e.network:
            return time.time() + (e.retry_after or 1.0)
        if e.rate_limited:
            return 0.0
        if e.code in ("not_on_map", "character_not_live"):
            # Waiting to be placed, or dead and waiting to respawn.
            self.mem.need_self = self.mem.need_position = True
            return time.time() + 1.0
        return time.time() + 1.0


def _fmt_intents(intents: list[dict] | None) -> str:
    if not intents:
        return "—"
    if len(intents) == 1:
        return _fmt_intent(intents[0])
    moves = sum(1 for i in intents if i.get("verb") == "SetPosition")
    return f"queue×{len(intents)} ({moves} moves)"


def _fmt_intent(i: dict | None) -> str:
    if i is None:
        return "—"
    verb = i["verb"]
    if verb == "SetPosition":
        return f"SetPosition({i['x']},{i['y']})"
    if verb == "Use":
        t = i["target"]
        return f"Use({t.get('kind')}:{t.get('character_id', '')})"
    if verb == "Take":
        return f"Take({i['supply_id']})"
    if verb == "WithdrawFromChest":
        return f"WithdrawFromChest({i['chest_id']}:{','.join(map(str, i.get('supply_ids', ['all'])))})"
    return verb


def _fmt_result(r: dict) -> str:
    if r.get("outcome") == "rejected":
        return f"rejected:{(r.get('rejection') or {}).get('code', '?')}"
    return r.get("outcome", "?")


def _fmt_event(e: dict) -> str:
    kind = e.get("kind", "?")
    if kind in ("SpokenTo", "BroadcastHeard"):
        return f'{kind} {e.get("speaker_id")}: "{e.get("text", "")}"'
    if kind == "Damaged":
        return f"Damaged {e.get('amount', 0)} by {e.get('source_kind', '?')}"
    if kind == "Died" and e.get("chest_id"):
        return f"Died, chest {e['chest_id']} at {e.get('map_id')}:{e.get('x')},{e.get('y')}"
    return kind
