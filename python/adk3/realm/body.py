"""The Agent Realm body: senses and acts within the call budget (docs/v3/SPEC.md **Interfaces**).

Every window spends one call: a read the world model is owed, else a round
trip. ``sense`` returns a State only for a round trip, so the loop decides
exactly when an action can go out. ``act`` sends the action's intents with
that round trip, or lets the action under way run on, and folds in what
comes back.
"""

from __future__ import annotations

import time

from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.executor import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    is_weapon_swing,
    queue_horizon_intents,
    step_landing,
)
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation.rejection import end_decision, learn_step_rejection
from agentrealm_adk.pacer import WINDOW_MARGIN, pacer_of
from agentrealm_adk.world import DOORS, WorldModel

from adk3.core.interfaces import Action

from . import actions, milestones
from .state import State

SELF_REFRESH = 60  # windows between self reads
ENTITY_REFRESH = 5  # ticks between entity reads
QUEUE_RESULT_SLACK = 5  # ticks past a queue's length before it is taken as run out unseen


class RealmBody:
    def __init__(self, client: Client, cid: int, out=print):
        self.client, self.cid, self.out = client, cid, out
        self.w = WorldModel(cid)
        self.m = Memory()
        self.pacer = pacer_of(client, cid)
        self.tick_hz, self.horizon = DEFAULT_TICK_RATE_HZ, queue_horizon_intents(
            tick_rate_hz=DEFAULT_TICK_RATE_HZ, horizon_seconds=DEFAULT_QUEUE_HORIZON_SECONDS)
        self.running: tuple | None = None  # key of the action whose queue runs
        self.not_before = 0.0
        self.last_attack_tick: int | None = None
        self.last_events: list[dict] = []
        self.last_results: list[dict] = []
        self.entrances: dict[int, list[tuple[int, int]]] = {}  # map id -> level entrance doors, from the minimap
        self.notes: list[str] = []  # what signs, scrolls and helpers said, oldest first
        self.town: tuple[int, int, int] | None = None  # (map id, x, y) from the world read
        self.no_effect: set[tuple[int, tuple[int, int]]] = set()  # (map, cell) a Use on did nothing

    # Startup.

    def start(self) -> dict:
        body = self.client.world(self.cid)
        self.tick_hz = max(1, int(body.get("tick_rate_hz", 1)))
        horizon_s = max(1, int(body.get("queue_horizon_seconds", DEFAULT_QUEUE_HORIZON_SECONDS)))
        self.horizon = queue_horizon_intents(tick_rate_hz=self.tick_hz, horizon_seconds=horizon_s)
        self.pacer.window = 1.0 / self.tick_hz
        if isinstance(body.get("level_count"), int):
            self.w.level_count = body["level_count"]
        town = body.get("town") or {}
        if {"map_id", "x", "y"} <= set(town):
            self.town = (int(town["map_id"]), int(town["x"]), int(town["y"]))
        return body

    # Sensing.

    def sense(self) -> State | None:
        self.pacer.wait_next_window(self.not_before)
        self.not_before = 0.0
        w, m = self.w, self.m
        w.tick += 1
        m.windows_since_self += 1
        call = self.choose_call()
        try:
            if call == "tick":
                if w.asleep:
                    self.round_trip([{"verb": "Wait"}])  # any intent wakes a sleeper
                    self.m.need_self = self.m.need_position = True
                    return None
                if w.pos is None or w.map_id is None:
                    self.round_trip(None)  # off the map: a poll brings the Respawned event
                    return None
                return State.of(w, self.running, self.tick_hz)
            self.read(call)
        except ApiError as e:
            self.on_error(call, e)
        return None

    def choose_call(self) -> str:
        w, m = self.w, self.m
        if m.need_self or m.windows_since_self >= SELF_REFRESH:
            return "self"
        if m.need_position or w.pos is None:
            return "tick" if w.asleep or not w.placed else "position"
        if w.terrain_stale():
            return "terrain"
        if w.map_id is not None and w.map_id not in w.map_levels and m.minimap_reads.get(w.map_id, 0) < 3:
            return "minimap"
        if w.tick - w.entities_tick >= ENTITY_REFRESH or w.entity_read_due(ENTITY_REFRESH):
            return "entities"
        return "tick"

    def read(self, call: str) -> None:
        w, m, c = self.w, self.m, self.client
        if call == "self":
            w.apply_self(c.self_(self.cid))
            m.need_self, m.windows_since_self = False, 0
        elif call == "position":
            w.apply_position(c.position(self.cid))
            m.need_position, m.path, m.warp_from = False, [], None
        elif call == "terrain":
            w.apply_terrain(c.terrain(self.cid, w.map_id, *w.perception_rect()))
        elif call == "entities":
            w.apply_entities(c.entities(self.cid, w.map_id, *w.perception_rect()))
        elif call == "minimap":
            m.minimap_reads[w.map_id] = m.minimap_reads.get(w.map_id, 0) + 1
            body = c.minimap(self.cid)
            w.apply_minimap(body)
            for mp in body.get("maps") or []:
                self.entrances[int(mp["map_id"])] = [(int(e["x"]), int(e["y"])) for e in mp.get("entrances") or []]

    def on_error(self, call: str, e: ApiError) -> None:
        self.out(f"{call}: error {e}")
        if e.status in (401, 403):
            raise e
        if e.code in ("not_on_map", "character_not_live"):
            self.resync()
        time.sleep(e.retry_after or 0)

    def resync(self) -> None:
        self.m.need_self = self.m.need_position = True
        self.drop_queue()

    # Acting.

    def act(self, a: Action | None) -> None:
        intents = None
        if a is not None and not (self.running == a.key and self.m.pending_intents is not None):
            intents = a.data.get("intents")
        try:
            self.round_trip(intents, a.key if a is not None and intents else None)
        except ApiError as e:
            self.on_error("tick", e)
        end_decision(self.m.nav, self.w.tick)

    def round_trip(self, intents: list[dict] | None, key: tuple | None = None) -> None:
        w, m = self.w, self.m
        r = self.client.tick(self.cid, intents, snapshot_version=w.snapshot_version)
        w.tick = int(r.get("tick", w.tick))
        if intents is not None:
            m.pending_intents = intents or None
            m.pending_queue = r.get("queue_id") if intents else None
            m.pending_next_index, m.queue_sent_tick = 0, w.tick
            self.running = key if intents else None
        self.last_results = r.get("intent_results") or []
        self.apply_results(self.last_results)
        earlier = w.entities
        self.last_events = w.apply_events(r.get("events_by_tick") or [])
        w.apply_observation(r.get("observation"))
        self.note_texts()
        w.note_level_clear(r.get("level_clear_ceremony"))
        w.learn_threat(self.last_events, earlier)
        if any(ev.get("kind") in ("Died", "Respawned") for ev in self.last_events):
            self.resync()
        finished = r.get("finished_queue")
        if finished and finished.get("queue_id") == m.pending_queue:
            self.drop_queue()
        elif m.pending_intents is not None and w.tick > m.queue_sent_tick + len(m.pending_intents) + QUEUE_RESULT_SLACK:
            self.drop_queue()
            m.need_position = True
        self.not_before = time.time() + int(r.get("window_remaining_ms", 0)) / 1000.0 + WINDOW_MARGIN

    def apply_results(self, results: list[dict]) -> None:
        """A Step applied moves us (a door sends us to read position); a swing starts the cooldown; a rejection ends the queue."""
        w, m = self.w, self.m
        for res in sorted(results, key=lambda x: (x.get("tick", 0), x.get("index", 0))):
            if m.pending_intents is None or res.get("queue_id") != m.pending_queue:
                continue
            idx = int(res.get("index", 0))
            if idx < m.pending_next_index or idx >= len(m.pending_intents):
                continue
            intent, tick = m.pending_intents[idx], int(res.get("tick", w.tick))
            if res.get("outcome") == "rejected":
                code = (res.get("rejection") or {}).get("code")
                if intent.get("verb") == "Step" and w.pos is not None:
                    learn_step_rejection(m, w, None, step_landing(w.pos, intent["direction"]), code, tick)
                self.out(f"rejected {intent.get('verb')}: {code}")
                self.drop_queue()
                m.need_position = True
                return
            m.pending_next_index = idx + 1
            target = intent.get("target") or {}
            if intent.get("verb") == "Use" and target.get("kind") == "block" and res.get("outcome") == "applied_no_effect":
                self.no_effect.add((w.map_id, (target["x"], target["y"])))
            if intent.get("verb") == "Step" and w.pos is not None:
                w.pos = step_landing(w.pos, intent["direction"])
                m.last_step_tick = tick
                if w.view.tiles.get(w.pos) in DOORS:
                    self.drop_queue()
                    m.need_position = True
                    return
            elif is_weapon_swing(intent, w.armed_code) and res.get("outcome") == "applied":
                self.last_attack_tick = tick

    def note_texts(self) -> None:
        """Keep what a Read or a helper's reply said: the strategist reads them."""
        for res in self.last_results:
            if res.get("text"):
                self.note(f"read: {res['text']}")
        for ev in self.last_events:
            kind = ev.get("kind")
            if kind == "Damaged":
                self.out(f"hit: -{ev.get('amount')} from {ev.get('source_kind')} {ev.get('source_id', '')}")
            elif kind in ("Died", "Respawned", "NPCDied"):
                self.out(f"event: {kind} {ev.get('npc_type', '')}")
            if kind == "SpokenTo" and ev.get("text"):
                self.note(f"{ev.get('speaker_kind', 'someone')} {ev.get('speaker_id', '')} said: {ev['text']}")

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes = (self.notes + [text])[-30:]

    def drop_queue(self) -> None:
        m = self.m
        m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
        self.running = None

    # What the loop asks of a body.

    def actions(self, s: State) -> list[Action]:
        return actions.common(s, self)

    def milestones(self, s: State) -> list[tuple[str, str]]:
        return milestones.shown(s)

    def surprises(self, before: State, s: State) -> list[str]:
        return milestones.surprises(before, s, self.last_events)

    def status(self, s: State) -> str:
        where = f"L{s.level}" if s.level else "overworld"
        return (f"hp {s.health}/{s.max_health} lives {s.lives} gems {s.gems} armed {s.armed or '-'} "
                f"@{where} {s.map_id}:{s.pos[0]},{s.pos[1]}")
