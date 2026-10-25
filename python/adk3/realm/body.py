"""The body: senses and acts for one character within its call budget, and learns from every answer.

Every tick window spends one call (Manual §7.4: one request per tick):
a read the world model is owed, else a round trip. ``sense`` returns the
moment only on a round trip's window, so the loop decides exactly when an
action can go out. ``act`` sends the action's intents with that round trip,
or lets the action still under way run on, and folds in what came back.

Everything worth keeping is written to memory as it arrives: what signs
and helpers said, entrances and where doors lead, shops seen, the gems our
cuts dropped, deaths, boss fights. Tiles and warps go to the ADK's world
knowledge base, where every agent's travel reads them.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.executor import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    is_weapon_swing,
    queue_horizon_intents,
    step_landing,
    ticks_per_step,
)
from agentrealm_adk.executor.pacing import attack_cooldown_ticks
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.knowledge_base.hostiles import load_hostiles, save_hostiles
from agentrealm_adk.knowledge_maps import record_locked_door, record_map_level, record_warp, sync_tiles, sync_world_maps
from agentrealm_adk.memory import Memory as NavMemory
from agentrealm_adk.navigation.rejection import end_decision, learn_step_rejection
from agentrealm_adk.pacer import WINDOW_MARGIN, pacer_of
from agentrealm_adk.survival import known_hostile
from agentrealm_adk.world import DOORS, Pos, WorldModel, chebyshev

from .actions import Action, Stop, Walk
from .memory import Attempt, Clue, Death, Memory
from .state import State

SELF_EVERY = 60  # windows between self reads
ENTITIES_EVERY = 5  # ticks between entity reads
QUEUE_SLACK = 5  # ticks past a queue's length before it is taken as run out unseen
CUT_DROP_TICKS = 40  # gems that come in this soon after a cut are its drop: it lands on the block, and we walk on
HELD_TICKS = 20  # a hostile standing this long without closing on us, with open ground toward us, is held
HELD_WITHIN = 8  # blocks: near enough that a chaser would be closing on us


class Body:
    def __init__(self, client: Client, cid: int, memory: Memory, kb: KnowledgeBase | None,
                 out: Callable[[str], None] = print):
        self.client, self.cid, self.memory, self.kb, self.out = client, cid, memory, kb, out
        self.world = WorldModel(cid)
        self.nav = NavMemory()  # the ADK's per-run memory: the queue in flight and what rejections taught the map
        self.pacer = pacer_of(client, cid)
        self.tick_hz = DEFAULT_TICK_RATE_HZ
        self.horizon = queue_horizon_intents(tick_rate_hz=self.tick_hz, horizon_seconds=DEFAULT_QUEUE_HORIZON_SECONDS)
        self.town: tuple[int, Pos] | None = None
        self.running: tuple | None = None  # key of the action whose queue is still running
        self.walk_left: list[Pos] = []  # cells the running walk has still to enter
        self.not_before = 0.0
        self.last_attack_tick: int | None = None
        self.last_speech_tick: int | None = None
        self.events: list[dict] = []  # this round trip's events
        self.carried_alive: list[str] = []  # what we carried at the last decision alive
        self.where_alive: tuple[int, Pos] = (0, (0, 0))
        self.surprises: list[str] = []  # what changed enough to tell the strategist, since the loop last took them
        self.cuts: dict[Pos, int] = {}  # blocks we broke on this map -> the tick, waiting to see a drop
        self.boss_fight: tuple[int, float] | None = None  # (level, when the fight began)
        self.stood_on: dict[str, set[str]] = {}  # hostile type -> block types seen under it this run
        self.gems_seen: int | None = None  # the gem counter at the last round trip
        self.last_kill: tuple[str, int] | None = None  # (type, tick) of the latest hostile we killed
        if kb is not None:
            load_hostiles(kb, self.world)

    # Startup and shutdown.

    def start(self) -> dict:
        body = self.client.world(self.cid)
        self.tick_hz = max(1, int(body.get("tick_rate_hz", DEFAULT_TICK_RATE_HZ)))
        seconds = max(1, int(body.get("queue_horizon_seconds", DEFAULT_QUEUE_HORIZON_SECONDS)))
        self.horizon = queue_horizon_intents(tick_rate_hz=self.tick_hz, horizon_seconds=seconds)
        self.pacer.window = 1.0 / self.tick_hz
        if isinstance(body.get("level_count"), int):
            self.world.level_count = body["level_count"]
        town = body.get("town") or {}
        if {"map_id", "x", "y"} <= town.keys():
            self.town = (int(town["map_id"]), (int(town["x"]), int(town["y"])))
        return body

    def save(self) -> None:
        self.memory.save()
        if self.kb is not None:
            sync_world_maps(self.kb, self.world)
            save_hostiles(self.kb, self.world, set())

    # Sensing.

    def sense(self) -> State | None:
        self.pacer.wait_next_window(self.not_before)
        self.not_before = 0.0
        w, nav = self.world, self.nav
        w.tick += 1
        nav.windows_since_self += 1
        call = self.next_call()
        try:
            if call == "tick":
                if w.asleep:
                    self.round_trip([{"verb": "Wait"}])  # any intent wakes a sleeper (Manual §11 Sleep)
                    nav.need_self = nav.need_position = True
                    return None
                if w.pos is None or w.map_id is None or not w.placed:
                    self.round_trip(None)  # downed or off the map: a poll brings Respawned
                    return None
                return self.state()
            self.read(call)
        except ApiError as e:
            self.on_error(call, e)
        return None

    def state(self) -> State:
        armed = self.world.armed_code
        if self.world.inventory_read and self.world.alive:
            self.carried_alive = [x.code for x in self.world.held_supplies] + ([armed] if armed else []) \
                + list(self.world.worn_codes.values())  # what a death would drop: the inventory resets by the Died event
            if self.world.pos is not None and self.world.map_id is not None:
                self.where_alive = (self.world.map_id, self.world.pos)  # where a death happens: unplaced by then
        return State.of(self.world, self.tick_hz, self.last_attack_tick, attack_cooldown_ticks(armed, self.tick_hz),
                        self.nav.last_step_tick, self.step_ticks(), self.last_speech_tick,
                        frozenset(self.memory.townsfolk))

    def step_ticks(self) -> int:
        return ticks_per_step(tick_rate_hz=self.tick_hz, movement_speed_milli=max(1, self.world.movement_speed))

    def next_call(self) -> str:
        w, nav = self.world, self.nav
        if nav.need_self or nav.windows_since_self >= SELF_EVERY:
            return "self"
        if nav.need_position or w.pos is None:
            return "tick" if w.asleep or not w.placed else "position"
        if w.terrain_stale():
            return "terrain"
        if w.map_id is not None and w.map_id not in w.map_levels and nav.minimap_reads.get(w.map_id, 0) < 2:
            return "minimap"
        if w.tick - w.entities_tick >= ENTITIES_EVERY or w.entity_read_due(ENTITIES_EVERY):
            return "entities"
        return "tick"

    def read(self, call: str) -> None:
        w, nav, c = self.world, self.nav, self.client
        if call == "self":
            w.apply_self(c.self_(self.cid))
            nav.need_self, nav.windows_since_self = False, 0
        elif call == "position":
            before = (w.map_id, w.pos)
            w.apply_position(c.position(self.cid))
            nav.need_position = False
            self.learn_warp(before)
        elif call == "terrain":
            t = c.terrain(self.cid, w.map_id, *w.perception_rect())
            w.apply_terrain(t)
            self.learn_terrain(t)
        elif call == "entities":
            w.apply_entities(c.entities(self.cid, w.map_id, *w.perception_rect()))
            self.learn_entities()
        elif call == "minimap":
            nav.minimap_reads[w.map_id] = nav.minimap_reads.get(w.map_id, 0) + 1
            body = c.minimap(self.cid)
            w.apply_minimap(body)
            self.learn_minimap(body)

    def on_error(self, call: str, e: ApiError) -> None:
        self.out(f"{call}: {e}")
        if e.status in (401, 403):
            raise e
        if e.code in ("not_on_map", "character_not_live"):
            self.resync()
        time.sleep(e.retry_after or 0)

    def resync(self) -> None:
        self.nav.need_self = self.nav.need_position = True
        self.drop_queue()

    # Acting.

    def act(self, action: Action | None) -> None:
        intents = None if action is None else self.intents_for(action)
        try:
            self.round_trip(intents, action.key if action is not None and intents is not None else None)
        except ApiError as e:
            self.on_error("tick", e)
        end_decision(self.nav.nav, self.world.tick)

    def intents_for(self, action: Action) -> list[dict] | None:
        """The intents to send for ``action``, or None to let what is running run on."""
        running = self.nav.pending_intents is not None and self.running == action.key
        if isinstance(action, Walk):
            if running and list(action.path[:len(self.walk_left)]) == self.walk_left[:len(action.path)]:
                return None
            last = self.nav.last_step_tick
            since = self.world.tick - last + 1 if last is not None and self.world.tick - last < 4 * self.step_ticks() else None
            return action.intents(self.world.pos, self.world.movement_speed, self.tick_hz, since)
        if running:
            return None
        if isinstance(action, Stop) and self.nav.pending_intents is None:
            return None
        return action.intents()

    def round_trip(self, intents: list[dict] | None, key: tuple | None = None) -> None:
        w, nav = self.world, self.nav
        r = self.client.tick(self.cid, intents, snapshot_version=w.snapshot_version)
        w.tick = int(r.get("tick", w.tick))
        if intents is not None:
            nav.pending_intents = intents or None
            nav.pending_queue = r.get("queue_id") if intents else None
            nav.pending_next_index, nav.queue_sent_tick = 0, w.tick
            self.running = key if intents else None
            self.walk_left = self.walk_cells(intents) if intents else []
        results = r.get("intent_results") or []
        self.apply_results(results)
        earlier = w.entities
        self.events = w.apply_events(r.get("events_by_tick") or [])
        w.apply_observation(r.get("observation"))
        w.learn_threat(self.events, earlier)
        self.learn_events(r.get("level_clear_ceremony"))
        self.learn_entities()
        if any(ev.get("kind") in ("Died", "Respawned") for ev in self.events):
            self.resync()
        finished = r.get("finished_queue")
        if finished and finished.get("queue_id") == nav.pending_queue:
            self.drop_queue()
        elif nav.pending_intents is not None and w.tick > nav.queue_sent_tick + len(nav.pending_intents) + QUEUE_SLACK:
            self.drop_queue()
            nav.need_position = True
        self.not_before = time.time() + int(r.get("window_remaining_ms", 0)) / 1000.0 + WINDOW_MARGIN

    def walk_cells(self, intents: list[dict]) -> list[Pos]:
        cells, here = [], self.world.pos
        for i in intents:
            if i.get("verb") == "Step" and here is not None:
                here = step_landing(here, i["direction"])
                cells.append(here)
        return cells

    def apply_results(self, results: list[dict]) -> None:
        """Match each result to what we sent (Manual §7.6): a Step applied moves us, a rejection ends the queue."""
        w, nav = self.world, self.nav
        for res in sorted(results, key=lambda x: (x.get("tick", 0), x.get("index", 0))):
            if nav.pending_intents is None or res.get("queue_id") != nav.pending_queue:
                continue
            idx = int(res.get("index", 0))
            if idx < nav.pending_next_index or idx >= len(nav.pending_intents):
                continue
            intent, tick, outcome = nav.pending_intents[idx], int(res.get("tick", w.tick)), res.get("outcome")
            self.learn_result(intent, res)
            if outcome == "rejected":
                code = (res.get("rejection") or {}).get("code")
                if intent.get("verb") == "Step" and w.pos is not None:
                    landing = step_landing(w.pos, intent["direction"])
                    learn_step_rejection(nav, w, self.kb, landing, code, tick)
                    self.learn_door_refusal(landing, code)
                if code not in ("movement_cooldown", "attack_cooldown"):
                    self.out(f"refused {intent.get('verb')}: {code}")
                self.drop_queue()
                nav.need_position = True
                return
            nav.pending_next_index = idx + 1
            if intent.get("verb") == "Step" and w.pos is not None:
                w.pos = step_landing(w.pos, intent["direction"])
                nav.last_step_tick = tick
                if self.walk_left:
                    self.walk_left.pop(0)
                if w.view.tiles.get(w.pos) in DOORS and w.map_id is not None:
                    nav.warp_from = (w.map_id, w.pos, w.view.tiles[w.pos])
                    self.drop_queue()
                    nav.need_position = True
                    return
            elif is_weapon_swing(intent, w.armed_code) and outcome == "applied":
                self.last_attack_tick = tick
            elif intent.get("verb") in ("Say", "Broadcast"):
                self.last_speech_tick = tick

    def drop_queue(self) -> None:
        nav = self.nav
        nav.pending_intents, nav.pending_queue, nav.pending_next_index = None, None, 0
        self.running, self.walk_left = None, []

    # Learning.

    def learn_terrain(self, t: dict) -> None:
        w = self.world
        if self.kb is None or w.map_id is None:
            return
        x0, y0, width, height = int(t["x0"]), int(t["y0"]), int(t["width"]), int(t["height"])
        tiles = w.maps[int(t["map_id"])].tiles
        sync_tiles(self.kb, int(t["map_id"]), {(x, y): tiles[(x, y)] for y in range(y0, y0 + height)
                                                for x in range(x0, x0 + width) if (x, y) in tiles})
        record_map_level(self.kb, w)
        for e in self.memory.entrances.values():
            if e.map_id == w.map_id and (e.x, e.y) in tiles and e.block != tiles[(e.x, e.y)]:
                e.block = tiles[(e.x, e.y)]
                e.locked = bool(w.view.locked.get((e.x, e.y)))
                self.memory.learned()

    def learn_minimap(self, body: dict) -> None:
        for m in body.get("maps") or []:
            map_id = int(m["map_id"])
            for mark in m.get("entrances") or []:
                self.memory.entrance(map_id, (int(mark["x"]), int(mark["y"])))
        w = self.world
        if w.map_level and w.map_id is not None:
            level = self.memory.level(w.map_level)
            if w.map_id not in level.maps:
                level.maps.append(w.map_id)
                self.surprises.append(f"inside level {w.map_level} (map {w.map_id})")
            for e in self.memory.entrances.values():
                if e.leads_to == w.map_id and e.level is None:
                    e.level, level.entrance = w.map_level, f"{e.map_id}:{e.x},{e.y}"
                    self.memory.learned()

    def learn_warp(self, before: tuple) -> None:
        """After a door step: where it led, kept for travel (the knowledge base) and for the entrances."""
        w, nav = self.world, self.nav
        warp, nav.warp_from = nav.warp_from, None
        if warp is None or w.map_id is None or w.pos is None or (w.map_id, w.pos) == warp[:2]:
            return
        from_map, from_pos, block = warp
        if self.kb is not None:
            record_warp(self.kb, from_map, from_pos, block, w.map_id, w.pos)
        entrance = self.memory.entrances.get(f"{from_map}:{from_pos[0]},{from_pos[1]}")
        if entrance is not None and entrance.leads_to != w.map_id:
            entrance.leads_to, entrance.reached = w.map_id, True
            self.memory.learned()
        self.surprises.append(f"went through the door at {from_pos[0]},{from_pos[1]} onto map {w.map_id}")
        boss = self.world_boss_room_entry(from_map, from_pos)
        if boss is not None:
            self.boss_fight = (boss, time.time())

    def world_boss_room_entry(self, from_map: int, from_pos: Pos) -> int | None:
        """The level whose boss door we just stepped through, if that door is the one memory names."""
        key = f"{from_map}:{from_pos[0]},{from_pos[1]}"
        return next((lv.number for lv in self.memory.levels.values() if lv.boss_door == key), None)

    def learn_door_refusal(self, cell: Pos, code: str | None) -> None:
        w = self.world
        entrance = self.memory.entrances.get(f"{w.map_id}:{cell[0]},{cell[1]}")
        if code == "door_locked" and self.kb is not None and w.map_id is not None:
            record_locked_door(self.kb, w.map_id, cell, w.view.tiles.get(cell, "framed_door"))
        if entrance is not None and code in ("door_locked", "boss_room_occupied", "over_strength_ceiling", "not_traversable"):
            if entrance.refused != code:
                entrance.refused, entrance.locked = code, entrance.locked or code == "door_locked"
                self.memory.learned()
                self.surprises.append(f"the entrance at {cell[0]},{cell[1]} refused us: {code}")

    def learn_entities(self) -> None:
        w, mem = self.world, self.memory
        if w.map_id is None or w.pos is None:
            return
        for e in w.entities:
            if e.kind == "npc" and e.pos in w.view.safe and not e.is_boss and not known_hostile(w, e):
                mem.saw_in_town(e.code)
            if e.kind == "chest" and e.id in w.chest_contents:
                mem.looked_in_chest(w.map_id, e.pos)
            if e.kind == "supply" and e.gem_price and not w.is_our_drop(e.id):
                mem.saw_for_sale(e.code, e.gem_price, w.map_id, e.pos)
            if e.kind == "npc" and e.is_boss and w.map_level:
                level = mem.level(w.map_level)
                if level.boss != e.code or level.boss_max_health != e.max_health:
                    level.boss, level.boss_max_health = e.code, e.max_health
                    mem.learned()
                    self.surprises.append(f"found level {w.map_level}'s boss: {e.code}, health {e.health}/{e.max_health}")
        self.learn_gems()
        self.cuts = {p: t for p, t in self.cuts.items() if w.tick - t <= CUT_DROP_TICKS}
        self.learn_territory()
        for e in mem.entrances.values():
            if e.map_id == w.map_id and not e.reached and chebyshev((e.x, e.y), w.pos) <= 1:
                e.reached = True
                mem.learned()

    def learn_gems(self) -> None:
        """Gems that came in soon after a cut or a kill came from it (a drop lands on the block, and we pick it up):
        credit the latest, the cut's region or the kill's type, which field work and fights are priced by."""
        w = self.world
        if w.gems is None or w.map_id is None:
            return
        gained = w.gems - self.gems_seen if self.gems_seen is not None else 0
        self.gems_seen = w.gems
        if gained <= 0:
            return
        cut = max(self.cuts.items(), key=lambda kv: kv[1]) if self.cuts else None
        kill = self.last_kill if self.last_kill and w.tick - self.last_kill[1] <= CUT_DROP_TICKS else None
        if kill is not None and (cut is None or kill[1] >= cut[1]):
            self.memory.kill_dropped(kill[0], gained)
            self.last_kill = None
        elif cut is not None:
            self.memory.dropped_gems(w.map_id, cut[0], gained)

    def learn_territory(self) -> None:
        """A hostile that stands still, not beside us, though open ground would bring it closer, stops at
        its zone's edge (Manual §11 Hostiles: a zone spawn never leaves its zone). Keep the ground it keeps to."""
        w = self.world
        for e in w.entities:
            if e.kind != "npc" or e.pos not in w.view.tiles:
                continue
            seen = self.stood_on.setdefault(e.code, set())
            seen.add(w.view.tiles[e.pos])
            if (w.pos is None or chebyshev(e.pos, w.pos) <= 1 or chebyshev(e.pos, w.pos) > HELD_WITHIN
                    or w.npc_still_ticks(e) < HELD_TICKS):
                continue
            closer = [n for n in w.open_neighbours(e.pos) if chebyshev(n, w.pos) < chebyshev(e.pos, w.pos)]
            if closer and all(w.view.tiles.get(n) not in seen for n in closer):
                self.memory.keeps_to(e.code, set(seen))

    def learn_result(self, intent: dict, res: dict) -> None:
        w, mem = self.world, self.memory
        if w.map_id is None:
            return
        verb, outcome, target = intent.get("verb"), res.get("outcome"), intent.get("target") or {}
        if verb == "Read" and target.get("kind") == "block":
            cell = (int(target["x"]), int(target["y"]))
            mem.read_sign(w.map_id, cell)
            if res.get("text") and mem.add_clue(Clue(res["text"], "sign", "", w.map_id, *cell)):
                self.out(f"read: {res['text']}")
                self.surprises.append("read a new sign")
        elif verb == "Say" and outcome != "rejected":
            npc = next((e for e in w.entities if e.id == intent.get("npc_id") and e.kind == "npc"), None)
            if npc is not None:
                mem.greeted(npc.code, w.map_id, npc.pos, answered=False)
        elif verb == "WithdrawFromChest" and outcome == "rejected":
            chest = next((e for e in w.entities if e.id == intent.get("chest_id")), None)
            if chest is not None:
                mem.changed_nothing(w.map_id, chest.pos)  # full, or not ours to take: not tried again
        elif verb == "Use" and target.get("kind") == "block":
            cell = (int(target["x"]), int(target["y"]))
            code = (res.get("rejection") or {}).get("code")
            if outcome == "rejected" and code not in (None, "attack_cooldown", "movement_cooldown", "target_out_of_range"):
                mem.use_refused(w.map_id, cell, code)
            elif outcome == "applied_no_effect":
                mem.changed_nothing(w.map_id, cell)
            elif outcome == "applied":
                mem.broke(w.map_id, cell)
                self.cuts[cell] = w.tick

    def learn_events(self, ceremony: dict | None) -> None:
        w, mem = self.world, self.memory
        for ev in self.events:
            kind = ev.get("kind")
            if kind == "SpokenTo" and ev.get("speaker_kind") == "npc" and ev.get("text"):
                npc = next((e for e in w.entities if e.kind == "npc" and e.id == ev.get("speaker_id")), None)
                code, pos = (npc.code, npc.pos) if npc is not None else ("helper", w.pos or (0, 0))
                if w.map_id is not None:
                    mem.greeted(code, w.map_id, pos, answered=True)
                    if mem.add_clue(Clue(ev["text"], "helper", code, w.map_id, *pos)):
                        self.out(f"{code} said: {ev['text']}")
                        self.surprises.append(f"a {code} told us something new")
            elif kind == "Died":
                map_id, at = self.where_alive
                carried = self.carried_alive
                mem.died(Death(time.time(), map_id, *at, killer=self.last_hitter(),
                               chest_id=ev.get("chest_id"), chest_map=ev.get("map_id"), chest_x=ev.get("x"),
                               chest_y=ev.get("y"), items=[c for c in carried if c != "pocket_knife"]))
                self.out(f"died, killed by {self.last_hitter() or 'something'}")
                self.surprises.append("we died")
                self.end_boss_fight("died")
            elif kind == "NPCDied" and self.last_attack_tick is not None and w.tick - self.last_attack_tick <= 20:
                code = ev.get("npc_type") or ""
                mem.killed(code)
                self.last_kill = (code, w.tick)
            elif kind == "Damaged":
                self.out(f"hit for {ev.get('amount')} by {ev.get('source_kind')} {ev.get('source_id', '')}")
        if ceremony:
            number = int(ceremony.get("level_number", 0))
            self.out(f"cleared level {number}")
            self.surprises.append(f"cleared level {number}")
            self.end_boss_fight("cleared", number)

    def end_boss_fight(self, result: str, number: int | None = None) -> None:
        if self.boss_fight is None and number is None:
            return
        level, began = self.boss_fight or (number, time.time())
        boss = next((e for e in self.world.entities if e.is_boss), None)
        self.memory.level(level).attempts.append(Attempt(time.time(), result, time.time() - began,
                                                         boss.health if boss is not None else None))
        self.memory.learned()
        self.boss_fight = None

    def last_hitter(self) -> str:
        if self.world.attacker is None:
            return ""
        kind, eid = self.world.attacker
        e = next((x for x in self.world.entities if x.kind == kind and x.id == eid), None)
        return e.code if e is not None else kind

    def take_surprises(self) -> list[str]:
        out, self.surprises = self.surprises, []
        return out

