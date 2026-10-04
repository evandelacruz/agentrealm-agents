"""The loop: one call per window, paced by the wall clock."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass

from .brain import Decision, Memory, choose_call, decide, path_blockers, remaining_path_stale, walkable_prefix
from .navigation.rejection import copy_nav, learn_step_rejection, on_block_changed
from .client import ApiError, Client
from .config import CharacterConfig
from .directives import DirectivesWatch, use_blocked_by_never_attack
from .item_table import absorb_attack_range, absorb_entities_payload, rejection_attack_range
from .knowledge_base import KnowledgeBase
from .executor import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    build_paced_walk_queue,
    pace_speech,
    pace_uses,
    queue_horizon_intents,
    step_landing,
    trim_to_horizon,
    wait,
)
from .poll_cadence import calm_poll_interval
from .world import DOORS, WorldModel, terrain_cells
from .zone_discovery import apply_town, apply_zone, zone_failed

# Land a little after a window opens, so a clock skew of a few ms does not put
# two calls in one window.
WINDOW_MARGIN = 0.05
# Ticks past a queue's own length to wait for its results before giving up.
QUEUE_RESULT_SLACK = 2


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
    def __init__(
        self,
        cfg: CharacterConfig,
        client: Client,
        character_id: int,
        stop: threading.Event,
        out=print,
        knowledge: KnowledgeBase | None = None,
    ):
        self.cfg = cfg
        self.client = client
        self.cid = character_id
        self.stop = stop
        self.out = out
        self.knowledge = knowledge
        self._reach_seen: int | None = None  # A18: reach from a rejection, filed after the observation
        self.world = WorldModel(character_id)
        self.mem = Memory()
        seed = cfg.policy.seed if cfg.policy.seed is not None else character_id
        self.rng = random.Random(seed)
        self.pacer = Pacer(1.0)
        self.tick_hz = DEFAULT_TICK_RATE_HZ
        self.queue_horizon_ticks = QUEUE_HORIZON_INTENTS
        cfg.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace = open(cfg.trace_path, "a", buffering=1)
        self.directives = DirectivesWatch(cfg.directives_path)
        self.directives.ensure_loaded()

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
        self.tick_hz = hz
        horizon_s = max(1, int(world.get("queue_horizon_seconds", DEFAULT_QUEUE_HORIZON_SECONDS)))
        self.queue_horizon_ticks = queue_horizon_intents(tick_rate_hz=hz, horizon_seconds=horizon_s)
        self.pacer = Pacer(1.0 / hz)
        apply_town(self.world, world.get("town"))
        self.log("world", f"{world.get('code')} {world.get('status')} {hz}Hz", {"world": world})
        not_before = 0.0
        try:
            while not self.stop.is_set():
                self.pacer.wait_next_window(not_before)
                not_before = 0.0
                # A window is one sim tick. Count it here: a skipped window
                # sends nothing, so no response would move the clock, and the
                # calm gap and entity_refresh would never come due. Responses
                # carry the server's tick and correct it.
                self.world.tick += 1
                if self.directives.maybe_reload():
                    self.log(
                        "directives",
                        f"reloaded never_attack={self.directives.directives.never_attack}",
                        {"directives": {"params": self.directives.directives.params, "never_attack": self.directives.directives.never_attack}},
                    )
                call = choose_call(self.world, self.mem, self.cfg.policy)
                if call == "skip":
                    self.mem.windows_since_self += 1
                    continue
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
            m.need_position, m.path = False, []
            self.log(call, "", {"position": p})
        elif call == "terrain":
            t = c.terrain(self.cid, w.map_id, *w.perception_rect())
            w.apply_terrain(t)
            w.tick = max(w.tick, int(t.get("tick", 0)))
            n = len(terrain_cells(t))
            self.note_held_path_stale()
            self.log(call, f"{n} cells, {len(w.view.tiles)} known", {"cells": n})
        elif call == "entities":
            e = c.entities(self.cid, w.map_id, *w.perception_rect())
            w.apply_entities(e)
            w.tick = max(w.tick, int(e.get("tick", 0)))
            m.alarm = False
            self._learn_items_from_entities(e)
            self.note_held_path_stale()
            seen = ", ".join(f"{x.kind}:{x.id}@{x.pos[0]},{x.pos[1]}" for x in w.entities) or "nobody"
            self.log(call, seen, {"entities": e})
        elif call == "zone":
            probe, m.zone_probe = m.zone_probe, None
            if probe is None:
                return 0.0  # choose_call picked no cell: spend nothing
            map_id, (x, y) = probe
            try:
                z = c.zone(self.cid, map_id, x, y)
            except ApiError as e:
                # A 4xx about the cell (unrevealed, out of bounds): drop it so
                # the next spare window probes another one. Transient and
                # character-level failures leave it to retry.
                if _cell_refused(e):
                    zone_failed(w, map_id, (x, y))
                raise
            fact = apply_zone(w, map_id, x, y, z)
            w.tick = max(w.tick, int(z.get("tick", 0)))
            self.log(call, f"@{map_id}:{x},{y} safe={fact.safe}", {"zone": z})
        else:
            return self.tick()
        return 0.0

    def tick(self) -> float:
        w, m = self.world, self.mem
        if m.cancel_queue:
            # The rest of the server queue was planned from a position that no
            # longer holds (a door moved us): replace it with nothing.
            d = Decision(None, "cancel queue")
            intents = []
            m.cancel_queue = m.resend_held_queue = False
        elif m.held_queue is not None:
            # Reflexes still run every round trip. One that fires drops the
            # held queue and its intent replaces it. A stale path resends a
            # fresh walk queue; anything else leaves the queue running.
            d = self.reflex_while_held()
            if d is not None:
                self.drop_held_queue()
                # Something must replace the held queue, or it keeps running.
                intents = self._apply_never_attack(self.intents_for(d)) or [wait()]
            elif m.resend_held_queue:
                # Position was re-read when the path went stale (choose_call
                # reads it before this poll), so the new walk starts from
                # where we are. With no walk to send, stop the old queue.
                self.clear_held_tracking()
                d = decide(
                    w,
                    m,
                    self.cfg.policy,
                    self.rng,
                    never_attack=self.directives.directives.never_attack,
                    knowledge=self.knowledge,
                )
                intents = self._apply_never_attack(self.intents_for(d))
                if intents:
                    d = Decision(d.intent, "path stale, resend")
                else:
                    d, intents = Decision(None, f"path stale, stop ({d.reason})"), []
            else:
                d = Decision(None, "queue held")
                intents = None
        else:
            m.resend_held_queue = False
            d = decide(
                w,
                m,
                self.cfg.policy,
                self.rng,
                never_attack=self.directives.directives.never_attack,
                knowledge=self.knowledge,
            )
            intents = self.intents_for(d)
            intents = self._apply_never_attack(intents)
        r = self.client.tick(self.cid, intents, snapshot_version=w.snapshot_version)
        w.tick = int(r.get("tick", w.tick))
        if intents:
            m.queue_sent_tick = w.tick
            if qid := r.get("queue_id"):
                m.pending_queue = qid
        rejected = self.apply_intent_results(r.get("intent_results") or [])
        earlier = w.entities
        events = w.apply_events(r.get("events_by_tick") or [])
        w.apply_observation(r.get("observation"))
        w.learn_threat(events, earlier)
        self._learn_items_from_tick(r.get("observation"))
        self.on_events(events)
        self.note_held_path_stale()
        if r.get("queue") and not rejected and not m.cancel_queue:
            # A rejection or a door already dropped our queue; an echoed server
            # queue on that same response must not bring the hold back.
            m.held_queue = r.get("queue")
        elif m.pending_intents is not None and m.pending_next_index < len(m.pending_intents):
            if w.tick > m.queue_sent_tick + len(m.pending_intents) + QUEUE_RESULT_SLACK:
                # The queue has had time to run out and its results never
                # matched: stop waiting on them rather than hold forever. We
                # may have walked without seeing it, so re-read position and
                # forget the path and step clock planned from the old one.
                m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
                m.held_queue = None
                m.need_position, m.path, m.last_step_tick = True, [], None
            else:
                m.held_queue = {"queue_id": m.pending_queue, "next_index": m.pending_next_index}
        else:
            m.held_queue = None
        m.last_poll_tick = w.tick
        m.calm_poll_interval = calm_poll_interval(w.tick, w.character_id)
        # One tick per intent still to run on the server: the calm gap never
        # outlasts it, so the character does not stand idle after the queue.
        if m.cancel_queue:
            m.queued_ticks = 0
        elif m.pending_intents is not None:
            m.queued_ticks = len(m.pending_intents) - m.pending_next_index
        else:
            m.queued_ticks = 1 if intents else 0
        m.hurt_last_poll = any(e.get("kind") == "Damaged" for e in events)
        detail = f"{_fmt_submit(intents, d)} ({d.reason})"
        if events:
            detail += " | " + ", ".join(_fmt_event(e) for e in events)
        if r.get("events_dropped"):
            detail += f" | dropped {r['events_dropped']}"
        self.log(
            "tick",
            detail,
            {
                "intents": intents,
                "reason": d.reason,
                "held_queue": m.held_queue,
                "events": events,
                "dropped": r.get("events_dropped", 0),
            },
        )
        # The intent resolves at this sim window's boundary. Do not call
        # again until it has closed, so the next submit lands in a new tick.
        return time.time() + int(r.get("window_remaining_ms", 0)) / 1000.0 + WINDOW_MARGIN

    def reflex_while_held(self) -> Decision | None:
        """A reflex (2–4b) that fires while a queue is held, else None.

        Only a firing reflex may touch the plan and the rng; they stay as they
        were otherwise, so the held queue's steps are not planned twice. The
        navigation learnings always stay as they were: this probe is not the
        decision window that ages them (A14).
        """
        m = self.mem
        saved = (list(m.path), m.goal, copy_nav(m.nav), self.rng.getstate())
        d = decide(
            self.world,
            m,
            self.cfg.policy,
            self.rng,
            never_attack=self.directives.directives.never_attack,
            knowledge=self.knowledge,
        )
        m.nav = saved[2]
        if d.reflex:
            return d
        m.path, m.goal = saved[0], saved[1]
        self.rng.setstate(saved[3])
        return None

    def note_held_path_stale(self) -> None:
        """Mark the held walk queue for replacement when its path went wrong.

        Its results after the replacement are no longer read, so position is
        re-read before the replan (A43).
        """
        m = self.mem
        if m.held_queue is None or m.resend_held_queue:
            return
        if remaining_path_stale(self.world, m, self.cfg.policy, self.knowledge):
            m.resend_held_queue = m.need_position = True

    def clear_held_tracking(self) -> None:
        """Stop waiting on the held queue's results without forgetting position."""
        m = self.mem
        m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
        m.pending, m.held_queue = None, None
        m.path, m.resend_held_queue = [], False

    def drop_held_queue(self) -> None:
        """Give up on the held queue. Its later results are no longer read, so
        where it took us is unknown: re-read position."""
        self.clear_held_tracking()
        self.mem.need_position = True

    def _apply_never_attack(self, intents: list[dict] | None) -> list[dict] | None:
        """Executor guard: drop any Use aimed at a never_attack target.

        A paced Use arrives behind its cooldown Waits, so the whole submit
        becomes one Wait: the server queue is replaced rather than left
        running, and none of the dropped queue is awaited.
        """
        if not intents:
            return intents
        blocked = self.directives.directives.never_attack
        if not blocked:
            return intents
        if not any(use_blocked_by_never_attack(i, self.world.entities, blocked) for i in intents):
            return intents
        m = self.mem
        m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
        m.pending = None
        return [wait()]

    def intents_for(self, d: Decision) -> list[dict] | None:
        """Movement, Use, and Say/Broadcast become paced queues; others stay one intent."""
        w, m = self.world, self.mem
        if d.intent is None:
            return None
        intent = d.intent
        verb = intent.get("verb")
        if verb != "SetPosition":
            if verb == "Use":
                return self._paced_action(intent, pace_uses, m.last_use_tick)
            if verb in ("Say", "Broadcast"):
                return self._paced_action(intent, pace_speech, m.last_speech_tick)
            m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
            m.pending = intent
            return [intent]
        if w.pos is None:
            return None
        target = (d.intent["x"], d.intent["y"])
        prefix = walkable_prefix(w, m, self.cfg.policy, m.path, self.knowledge)
        on_path = bool(prefix) and prefix[0] == target
        cells = list(prefix) if on_path else [target]
        # Ticks since the last applied Step, counted to the latest tick we
        # know of: the first intent runs no earlier, so the owed Waits are
        # never too few and the first Step never draws movement_cooldown.
        since = None if m.last_step_tick is None else max(1, w.tick - m.last_step_tick)
        intents = build_paced_walk_queue(
            w.pos,
            cells,
            movement_speed_milli=w.movement_speed,
            tick_rate_hz=self.tick_hz,
            ticks_since_last_step=since,
        )
        intents = trim_to_horizon(intents, limit=self.queue_horizon_ticks)
        # Waits after the last Step that fits only idle: the next queue opens
        # with whatever is still owed instead.
        while intents and intents[-1]["verb"] == "Wait":
            intents.pop()
        queued = sum(1 for i in intents if i["verb"] == "Step")
        if not queued:
            return None
        if on_path:
            m.path = m.path[queued:]
        m.pending_intents, m.pending_queue, m.pending_next_index = intents, None, 0
        m.pending = None
        m.path_blockers = path_blockers(w, m, self.cfg.policy, self.knowledge)
        return intents

    def _paced_action(self, intent: dict, pace, last_tick: int | None) -> list[dict] | None:
        """``intent`` behind the Waits its cooldown still owes, cut at the horizon.

        None when the cooldown outlasts the horizon: nothing is sent this
        round trip, and the trace says why.
        """
        w, m = self.world, self.mem
        since = None if last_tick is None else max(1, w.tick - last_tick)
        paced = trim_to_horizon(pace([intent], ticks_since_last=since), limit=self.queue_horizon_ticks)
        while paced and paced[-1]["verb"] == "Wait":
            paced.pop()
        if not paced:
            self.log(
                "pace",
                f"{_fmt_intent(intent)} held: cooldown outlasts the {self.queue_horizon_ticks}-tick horizon",
                {"held": intent, "ticks_since_last": since, "horizon": self.queue_horizon_ticks},
            )
            return None
        if len(paced) == 1:
            m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
            m.pending = paced[0]
        else:
            m.pending_intents, m.pending_queue, m.pending_next_index = paced, None, 0
            m.pending = None
        return paced

    def apply_intent_results(self, results: list[dict]) -> bool:
        """Fold intent results since the last call. True if the last one rejected."""
        w, m = self.world, self.mem
        if not results:
            return False
        rejected = False
        for res in sorted(results, key=lambda r: (r.get("tick", 0), r.get("index", 0))):
            if m.pending_queue is None and m.pending_intents is None and m.pending is None:
                break
            if not self._result_is_ours(res):
                continue
            idx = int(res.get("index", 0))
            if m.pending_intents is not None and idx < m.pending_next_index:
                continue
            intent = self._intent_at(idx)
            if self.on_result(res, idx):
                self._note_reach(res, intent)
                rejected = True
                break
            m.pending_next_index = idx + 1
        if m.pending_intents is not None and m.pending_next_index >= len(m.pending_intents):
            m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
        return rejected

    def _result_is_ours(self, res: dict) -> bool:
        # Only the queue_id our own submit was answered with: a result for any
        # other queue (an earlier one we gave up on) is never adopted.
        m = self.mem
        qid = res.get("queue_id")
        if qid is None or qid != m.pending_queue:
            return False
        return m.pending_intents is not None or res.get("index", 0) == 0

    def _intent_at(self, index: int) -> dict | None:
        m = self.mem
        if m.pending_intents is not None and index < len(m.pending_intents):
            return m.pending_intents[index]
        if m.pending is not None and index == 0:
            return m.pending
        return None

    def on_result(self, result: dict, index: int) -> bool:
        """Applies one intent result. True when it was rejected."""
        w, m = self.world, self.mem
        intent = self._intent_at(index)
        if result.get("outcome") != "rejected":
            if intent and intent.get("verb") == "Step" and w.pos is not None:
                w.pos = step_landing(w.pos, intent["direction"])
                m.last_step_tick = int(result.get("tick", w.tick))
                if w.view.tiles.get(w.pos) in DOORS:
                    # A door moves us; the Steps still queued behind this one
                    # would walk from the wrong place.
                    m.need_position, m.path = True, []
                    m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
                    m.held_queue, m.cancel_queue = None, True
            if intent and intent.get("verb") == "Use":
                m.last_use_tick = int(result.get("tick", w.tick))
            if intent and intent.get("verb") in ("Say", "Broadcast"):
                m.last_speech_tick = int(result.get("tick", w.tick))
            if m.pending is not None and index == 0:
                m.pending = None
            return False
        if intent and intent.get("verb") == "Step" and w.pos is not None:
            rej = result.get("rejection") or {}
            learn_step_rejection(
                m,
                w,
                self.knowledge,
                step_landing(w.pos, intent["direction"]),
                rej.get("code"),
                int(result.get("tick", w.tick)),
            )
        m.pending = None
        m.pending_intents = None
        m.pending_queue = None
        m.pending_next_index = 0
        m.path, m.need_position = [], True
        if (result.get("rejection") or {}).get("category") == "state":
            m.need_self = True
        return True

    def _with_item_table(self, fn) -> None:
        kb = self.knowledge
        if kb is None:
            return
        with kb.lock:
            fn(kb.items)

    def _learn_items_from_entities(self, payload: dict) -> None:
        self._with_item_table(lambda items: absorb_entities_payload(items, payload))

    def _note_reach(self, result: dict, intent: dict | None) -> None:
        # Held until the same response's observation is applied: an Arm that
        # resolved earlier in this response is only in that inventory, and
        # get_self's attack_range can trail an Arm (B100), so neither the
        # previous loadout nor get_self says which weapon this reach belongs to.
        if intent and intent.get("verb") == "Use":
            self._reach_seen = rejection_attack_range(result)

    def _learn_items_from_tick(self, obs: dict | None) -> None:
        w = self.world
        reach, self._reach_seen = self._reach_seen, None

        def learn(items: dict) -> None:
            if obs and not obs.get("unchanged"):
                body = obs.get("snapshot") if obs.get("complete") else obs.get("delta")
                if isinstance(body, dict) and "entities" in body:
                    absorb_entities_payload(items, body.get("entities"))
            absorb_attack_range(items, w.armed_code, reach)

        self._with_item_table(learn)

    def on_events(self, events: list[dict]) -> None:
        w, m = self.world, self.mem
        for ev in events:
            kind = ev.get("kind")
            # These happen to the queue owner and carry no subject_id (docs/API.md, Events).
            if kind in ("Damaged", "Attacked"):
                m.alarm = True
            if kind == "Died":
                m.need_self = m.need_position = True
                m.path, m.last_step_tick = [], None
                m.last_use_tick = m.last_speech_tick = None
                m.pending_intents = m.pending = m.pending_queue = None
                m.pending_next_index = 0
                m.held_queue, m.resend_held_queue = None, False
        # WorldModel.apply_events already parsed BlockChanged (A14).
        for map_id, p in w.changed_blocks:
            on_block_changed(m, map_id, p)

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


def _cell_refused(e: ApiError) -> bool:
    """A zone read refused for the cell itself, not for the character or the line."""
    if e.network or e.paused or e.rate_limited:
        return False
    if e.status in (401, 403) or e.status >= 500:
        return False
    return e.code not in ("not_on_map", "character_not_live")


def _fmt_submit(intents: list[dict] | None, d: Decision) -> str:
    if intents is None:
        return "—" if d.intent is None else _fmt_intent(d.intent)
    if len(intents) == 1:
        return _fmt_intent(intents[0])
    steps = sum(1 for i in intents if i.get("verb") == "Step")
    waits = sum(1 for i in intents if i.get("verb") == "Wait")
    return f"queue {steps}×Step {waits}×Wait"


def _fmt_intent(i: dict | None) -> str:
    if i is None:
        return "—"
    verb = i["verb"]
    if verb == "SetPosition":
        return f"SetPosition({i['x']},{i['y']})"
    if verb == "Step":
        return f"Step({i['direction']})"
    if verb == "Wait":
        return "Wait"
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
