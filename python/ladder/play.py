"""Lorraine's loop (A141): one call a window, her reflexes first, then the tree's op over the ADK.

Every window spends one call (``choose_call``): a read the world model is
owed, else a round trip (``POST tick``). Each round trip:

1. **Reflexes** (``ladder/reflexes.py``): sync, downed, flee, retreat, heal,
   fight back, each on her one risk function. The first that fires with
   something to send holds the round: its intents go out and the op and the
   goal are left as they were, so the next walk of the tree decides.
2. **A walk under way** runs on: the queue an op sent is not sent again
   while the op is unchanged and the queue still has intents to run.
3. **The op** for the turn's goal (``goal_op``): every round the tree is
   walked from the top (``goals.walk``) after lifting the blocks whose
   awaited state came (``ask.lift_changed``). The planner is asked the one
   question (A140) only when the goal or the state a block may wait on
   changes (``ask.watch_lines``), or after an ask with no answer once the
   backoff and budget allow. The call runs off the tick thread
   (``ask.Planner.start``): until its reply is back the tree's own op runs,
   so a slow or hung call never holds up a reflex. A ``cannot`` blocks the
   node and the next walk descends.
4. **Carried out** by ``ladder/act.py``: the op's walk and intents on the
   ADK's executor and pathing. An op over (done, or nothing left it can do)
   is not run again for that goal, and the tree is asked afresh.
5. **No silent hold** (A168): an op that sends nothing the same way two
   rounds in a row is over, but for a wait the clock clears (a weapon
   cooldown, her place unread, occupants on the way: ``Act.waits``), which
   is neither a repeat nor a stall. Every round the op sends nothing and no reflex
   holds is a stall, but for one with the question about the goal still
   out; ``STALL_ROUNDS`` of them in a row block the goal with why, and the
   next walk asks the planner once, saying so.
6. **A blocked goal never idles her** (A169): the walk works the tree's
   other nodes under it ("gather" always has an op), and the blocked goal
   is retried once its blocker can have changed (``ask.lift_changed``: the
   state the planner saw, a walk given up for it walkable again, its
   expiry). A walk given up holds until the ground round it changes, never
   on time alone (A171): while it holds, the goal stays blocked and the
   planner is asked once for another way. With no
   goal at all and the world not beaten, she always asks the planner about
   the blocked node, with why; exploring (``IDLE_OP``) only fills the wait
   for the answer. An answer that walks back to a target given up and still
   backed off is not run (``refused``). Each tick record names the goals
   blocked and why (``round_record``), beside the goal worked.

Every send, a reflex's or an op's, goes through the one water gate
(``fording.ford_gate``, A133), as Gary's do.

What a round trip brings back is folded in by the world model; the queue
she sent is tracked by its results (a ``Step`` applied moves her, a door
sends her to read position), as the ADK's held-queue rules read them
(``executor.held_queue``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from agentrealm_adk import config
from agentrealm_adk.client import ApiError, Client
from agentrealm_adk.executor import (
    DEFAULT_QUEUE_HORIZON_SECONDS,
    DEFAULT_TICK_RATE_HZ,
    QUEUE_HORIZON_INTENTS,
    is_weapon_swing,
    queue_horizon_intents,
    step_landing,
)
from agentrealm_adk.fording import ford_gate, loadout_memory
from agentrealm_adk.executor.held_queue import STEP_DOUBT, open_step, possible_positions, unconfirmed_send
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.knowledge_base.death_chests import load_death_chests, save_death_chests
from agentrealm_adk.knowledge_base.hostiles import load_hostiles, save_hostiles
from agentrealm_adk.knowledge_maps import record_map_level, record_map_levels, record_warp, sync_tiles, sync_world_maps
from agentrealm_adk.level_order import minimap_due, note_door_step, settle_entry
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation.rejection import end_decision, learn_step_rejection
from agentrealm_adk.navigation.stuck import given_up_dests, is_backed_off, note_ground
from agentrealm_adk.navigation.stuck import on_rejection as nav_on_rejection
from agentrealm_adk.navigation.stuck import on_step as nav_on_step
from agentrealm_adk.pacer import WINDOW_MARGIN, pacer_of
from agentrealm_adk.survival import recently_attacked
from agentrealm_adk.targets import EXPLORE_ANYWHERE, GoalOp
from agentrealm_adk.trace import Trace, tick_detail
from agentrealm_adk.travel.knowledge import load_our_drops, record_shop_cell, save_our_drops, sync_entrances, sync_town
from agentrealm_adk.world import DOORS, Passage, WorldModel
from agentrealm_adk.zone_discovery import apply_town

from .act import FETCH_GOAL, OWN_WALKS, Act, Ctx, Started, act, given_up_lines, given_up_target
from .ask import ALREADY_MET, Call, Planner, Reply, already_met, answer, lift_changed, seen, watch_lines
from .goals import Blocks, Node, Turn, tree, walk
from .readiness import ORDINARY
from .reflexes import ESCAPES, reflex, threats

# Windows between self reads.
SELF_REFRESH = 60
# Ticks past a queue's length to wait for it to finish before reading position again.
QUEUE_RESULT_SLACK = 5
# Round trips in a row the op may send nothing, no reflex holding, before its goal is blocked (A168).
STALL_ROUNDS = 10
# What she works with no goal while a block stands and the world is not beaten (A169).
IDLE_OP: GoalOp = {"op": "explore_area", "radius": EXPLORE_ANYWHERE}


def choose_call(w: WorldModel, m: Memory, policy: config.Policy) -> str:
    """Which call this window spends: the read the world model is owed most, else a round trip."""
    if m.need_self or m.windows_since_self >= SELF_REFRESH:
        return "self"
    if m.need_position or w.pos is None:
        # Asleep or off the map, a position read cannot answer: a round trip
        # wakes her (sync) or brings the Respawned event.
        return "tick" if w.asleep or not w.placed else "position"
    if w.terrain_stale():
        return "terrain"
    if minimap_due(w, m):
        return "minimap"
    if w.tick - w.entities_tick >= policy.entity_refresh or w.entity_read_due(policy.entity_refresh):
        return "entities"
    return "tick"


@dataclass(frozen=True)
class Decision:
    """What a round trip sends (None polls and lets the queue run), why, and who decided."""

    intents: list[dict] | None
    why: str
    by: str
    stalled: bool = False  # the turn's op sent nothing and no reflex held the round (A168)
    reflex: str | None = None  # the reflex that held the round, if one did


# Said when a goal is blocked on a walk given up (A171): nothing round it
# changed, so it is not walked again, and the planner is asked for another way.
GIVEN_UP_NOTE = (
    "its walk was given up and nothing round it has changed since, so it is not walked again: "
    "answer with another way (explore toward it, another target, or what opens the way), or cannot"
)


class LadderRunner:
    """Lorraine's own loop on the ADK: reflexes, then the tree's op."""

    def __init__(self, cfg: config.CharacterConfig, client: Client, cid: int, stop, planner: Planner,
                 knowledge: KnowledgeBase | None = None, out=print):
        self.cfg, self.client, self.cid, self.stop = cfg, client, cid, stop
        self.planner, self.knowledge = planner, knowledge
        self.failed = False  # set when it stopped on an error, not on Ctrl-C
        self.world = WorldModel(cid)
        self.mem = Memory()
        self.pacer = pacer_of(client, cid)
        self.tick_hz, self.horizon = DEFAULT_TICK_RATE_HZ, QUEUE_HORIZON_INTENTS
        self.trace = Trace(cfg.trace_path(cid), cfg.profile, out)
        self._loaded_hostiles = load_hostiles(knowledge, self.world)
        load_our_drops(knowledge, self.world)  # supplies this character dropped: no shop's (A158)
        load_death_chests(knowledge, self.world)  # chests earlier deaths left: a source of items (A122, A167)
        self.root: Node = tree()
        self.blocks = Blocks()
        self.turn: Turn | None = None  # the last walk of the tree
        self.op: GoalOp | None = None
        self.started: Started | None = None  # where the op stood when it began
        self.asked_for: tuple | None = None  # the goal and the state the last ask was about
        self.answered = False  # the planner answered that ask
        self.done: list[GoalOp] = []  # ops over since that ask: never run again for it
        self.call: Call | None = None  # the question out, off the tick thread
        self.met: Reply | None = None  # a cannot already met, asked about once more
        self.queue_of: str | None = None  # who sent the queue still running: a reflex, or the op
        self.empty: tuple[str | None, str] | None = None  # the op and why of the last round it sent nothing
        self.stalls = 0  # rounds in a row the op sent nothing, no reflex holding (A168)
        self.note = ""  # said with the next ask: why the last goal was blocked
        self.asked_blocked: tuple[str, int] | None = None  # the blocked node last asked about, and its block's expiry
        self.own_blocks: set[str] = set()  # nodes the loop blocked itself (a stall, every op over), not a cannot
        self.goal_since: tuple[str | None, int] = (None, 0)  # the turn's goal and the tick it became the goal
        self.last_given_up: tuple[str, ...] = ()  # the walks the goal's last op was over on, given up (A171)

    def log(self, call: str, detail: str, record: dict | None = None) -> None:
        self.trace.log(self.world, call, detail, record or {})

    # The loop.

    def run(self) -> None:
        w = self.world
        try:
            body = self.client.world(self.cid)
        except ApiError as e:
            self.log("world", f"read failed: {e}")
            self.failed = True
            self.trace.close()
            return
        self.tick_hz = max(1, int(body.get("tick_rate_hz", 1)))
        horizon_s = max(1, int(body.get("queue_horizon_seconds", DEFAULT_QUEUE_HORIZON_SECONDS)))
        self.horizon = queue_horizon_intents(tick_rate_hz=self.tick_hz, horizon_seconds=horizon_s)
        self.pacer.window = 1.0 / self.tick_hz
        apply_town(w, body.get("town"))
        if isinstance(body.get("level_count"), int):
            w.level_count = body["level_count"]
        if self.knowledge is not None:
            sync_town(self.knowledge, body.get("town"))
        self.log("world", f"{body.get('code')} {self.tick_hz}Hz", {"world": body})
        not_before = 0.0
        try:
            while not self.stop.is_set():
                self.pacer.wait_next_window(not_before)
                w.tick += 1
                self.mem.windows_since_self += 1
                call = choose_call(w, self.mem, self.cfg.policy)
                try:
                    not_before = self.step(call)
                except ApiError as e:
                    if not self.on_error(call, e):
                        self.failed = True
                        return
        finally:
            if self.knowledge is not None:
                sync_world_maps(self.knowledge, w)
                save_hostiles(self.knowledge, w, self._loaded_hostiles)
                save_our_drops(self.knowledge, w)
                save_death_chests(self.knowledge, w)
            self.trace.close()

    def on_error(self, call: str, e: ApiError) -> bool:
        """Log an API error and react; False stops play."""
        self.log(call, f"error {e}")
        if e.status in (401, 403):
            self.log(call, "stopping: API key rejected or not allowed to play this character")
            return False
        if e.code in ("not_on_map", "character_not_live"):
            self.resync()
        self.stop.wait(e.retry_after or 0)
        return True

    def resync(self) -> None:
        """Forget the queue and the path, and read self and position again (death, respawn, a door)."""
        m = self.mem
        m.need_self = m.need_position = True
        m.path = []
        self.drop_queue()

    def step(self, call: str) -> float:
        """Spend this window's call; the earliest time for the next one."""
        w, m, c, kb = self.world, self.mem, self.client, self.knowledge
        if call == "self":
            w.apply_self(c.self_(self.cid))
            m.need_self, m.windows_since_self = False, 0
            self.log(call, f"alive={w.alive} health={w.health}/{w.max_health}")
        elif call == "position":
            w.apply_position(c.position(self.cid))
            self.note_warp_landing()
            m.need_position, m.path = False, []
            self.log(call, "")
        elif call == "terrain":
            t = c.terrain(self.cid, w.map_id, *w.perception_rect())
            w.apply_terrain(t)
            if kb is not None:
                # The cells of this read's window into the knowledge base (A26).
                x0, y0, width, height = int(t["x0"]), int(t["y0"]), int(t["width"]), int(t["height"])
                tiles = w.maps[int(t["map_id"])].tiles
                window = {(x, y): tiles[(x, y)] for y in range(y0, y0 + height) for x in range(x0, x0 + width) if (x, y) in tiles}
                sync_tiles(kb, int(t["map_id"]), window)
                record_map_level(kb, w)
            self.log(call, f"{len(w.view.tiles)} known")
        elif call == "entities":
            e = c.entities(self.cid, w.map_id, *w.perception_rect())
            w.apply_entities(e)
            self.learn_shops()
            self.log(call, ", ".join(f"{x.kind}:{x.id}@{x.pos[0]},{x.pos[1]}" for x in w.entities) or "nobody")
        elif call == "minimap":
            if w.map_id is not None:
                m.minimap_reads[w.map_id] = m.minimap_reads.get(w.map_id, 0) + 1
            body = c.minimap(self.cid)
            w.apply_minimap(body)
            if kb is not None:
                sync_entrances(kb, body)
                record_map_levels(kb, w.map_levels)
            settle_entry(kb, w, m)
            self.log(call, f"map {w.map_id} level={w.map_level}")
        else:
            return self.tick()
        return 0.0

    def tick(self) -> float:
        w = self.world
        d = self.decide()
        r = self.client.tick(self.cid, d.intents, snapshot_version=w.snapshot_version)
        events = self.apply(r, d)
        self.log("tick", tick_detail(d.intents, None, f"{d.by}: {d.why}", events, r.get("events_dropped", 0)),
                 {"intents": d.intents, "by": d.by, "reason": d.why, "events": events, **self.round_record(d)})
        # The intents resolve at this window's end: the next round trip lands in a new tick.
        return time.time() + int(r.get("window_remaining_ms", 0)) / 1000.0 + WINDOW_MARGIN

    # Deciding a round.

    def ctx(self) -> Ctx:
        goal = self.turn.goal if self.turn is not None else None
        if self.started is None:
            self.started = Started.now(self.world)
        return Ctx(self.world, self.mem, self.knowledge, self.cfg.policy, self.started, self.tick_hz, self.horizon,
                   goal.value if goal is not None else ORDINARY)

    def decide(self) -> Decision:
        """The round's decision (``choose``), watched for stalls (A168): a
        round the op sent nothing and no reflex held counts one, any other
        round ends the run of them, and ``STALL_ROUNDS`` in a row block the
        goal (``stalled``). A round with the question about the turn's goal
        still out is not counted, so a slow reply is never thrown away."""
        note_ground(self.mem.nav_stuck, self.world)  # a give-up holds until its ground changes (A171)
        d = self.choose()
        end_decision(self.mem.nav, self.world.tick)  # a rejection's lessons last one decision (A14, A169)
        if not d.stalled:
            self.stalls = 0
            return d
        if self.asking_about_goal():
            return d  # waiting on the planner is no stall: its answer may be the op
        self.stalls += 1
        if self.stalls >= STALL_ROUNDS:
            self.stalled(d.why)
        return d

    def choose(self) -> Decision:
        """The reflexes first; then the walk under way; then the op for the
        turn's goal. Everything sent goes through the water gate (``gate``).
        An op that sends nothing with the same why as the round before is
        over (A168), so no empty round repeats, but for a wait the clock
        clears (``Act.waits``: a cooldown, her place unread), which is no stall."""
        saved = loadout_memory(self.mem)
        fired = reflex(self.ctx(), lambda name, intents: self.gate(intents, saved, escape=name in ESCAPES)[0])
        if fired is not None:
            if self.running() and self.queue_of == fired.name:
                return Decision(None, f"{fired.why}: queue under way", fired.name, reflex=fired.name)
            if fired.intents is None and self.running():
                # A hold stops what another sent: the op's walk does not run on under it.
                self.drop_queue()
                return Decision([], f"{fired.why}: stop", fired.name, reflex=fired.name)
            return replace(self.send(fired.intents, fired.why, fired.name), reflex=fired.name)
        op = self.goal_op()
        if self.running() and self.queue_of == op_key(op):
            return Decision(None, "queue under way", op["op"] if op else "tree")
        for _ in range(2):  # an op over makes way for the next at once
            if op is None:
                break
            a = act(op, self.ctx())
            if not a.over:
                intents, cut = self.gate(a.intents, saved)
                why = f"{a.why}; {cut}" if cut else a.why
                if intents:
                    self.empty = None
                    return self.send(intents, why, op["op"], op_key(op))
                if a.waits and not cut:  # the clock clears it: no stall, and no repeat ends the op
                    self.empty = None
                    return Decision(None, why, op["op"])
                if self.empty != (op_key(op), why):
                    self.empty = (op_key(op), why)
                    return Decision(None, why, op["op"], stalled=True)
                a = Act(None, f"{why}, again", over=True)
            self.log("op", f"{op['op']} over: {a.why}")
            if a.given_up:
                self.last_given_up = a.given_up
            self.end_op(a.why)
            op = self.goal_op()
        return Decision(None, self.nothing_to_work(), "tree", stalled=self.turn is not None and self.turn.goal is not None)

    def nothing_to_work(self) -> str:
        """Why no op works this round: the blocks that stand, if any (A169)."""
        skipped = self.turn.skipped if self.turn is not None else []
        if not skipped:
            return "no op to work"
        node, block = skipped[0]
        return f"no op to work: {node.title} blocked, {block.reason}"

    def asking_about_goal(self) -> bool:
        """The question about the turn's goal is out and not yet taken up."""
        goal = self.turn.goal if self.turn is not None else None
        return goal is not None and self.call is not None and self.call.node.key == goal.key

    def round_record(self, d: Decision) -> dict:
        """What a tick record names besides its intents (A168): the tree's
        goal node, the goals blocked and why (A169: the goal worked is the
        one worked instead), the op, the act's result (why, whether
        anything went out, a stall) and the reflex that held the round, if one did."""
        goal = self.turn.goal if self.turn is not None else None
        skipped = self.turn.skipped if self.turn is not None else []
        return {
            "goal": goal.key if goal is not None else None,
            "blocked": [{"goal": n.key, "why": b.reason} for n, b in skipped],
            "op": self.op,
            "act": {"why": d.why, "sent": bool(d.intents), "stalled": d.stalled, "stalls": self.stalls},
            "reflex": d.reflex,
        }

    def gate(self, intents: list[dict] | None, saved: dict, *, escape: bool = False) -> tuple[list[dict] | None, str]:
        """The one water gate every send goes through (``fording.ford_gate``,
        A133): a fording supply armed before the first step onto water, a
        queue that would strand her afloat cut short, the weapon armed again
        ashore (never in front of an escape). Returns what goes out, and why
        it was cut ("" when it was not)."""
        if not intents:
            return intents, ""
        return ford_gate(self.world, self.mem, saved, intents, self.knowledge, rearm=not escape)

    def send(self, intents: list[dict] | None, why: str, by: str, owner: str | None = None) -> Decision:
        """Send ``intents`` in place of the queue running, or poll when there are none.

        A queue still running may have walked her on unseen, so what replaces
        it must be sound from every cell she may stand on (``held_queue``,
        A116): else, under attack, the best open Step; otherwise a stop, with
        position read again first."""
        if intents is None:
            return Decision(None, why, by)
        w, m = self.world, self.mem
        if self.running():
            starts = possible_positions(w, m)
            doubt = unconfirmed_send(w, m, self.cfg.policy, self.knowledge, intents, starts)
            if doubt == STEP_DOUBT and recently_attacked(w):
                safer = open_step(w, m, self.cfg.policy, self.knowledge, intents, starts, [e.pos for e in threats(self.ctx())])
                intents, doubt = (safer, None) if safer else (intents, doubt)
            if doubt is not None:
                self.drop_queue()
                m.need_position = True
                return Decision([], f"{why}: stop, {doubt}", by)
        self.queue_of = owner or by
        return Decision(intents, why, by)

    def running(self) -> bool:
        return self.mem.pending_intents is not None

    # The op for the turn's goal.

    def goal_op(self) -> GoalOp | None:
        """The op for the turn's goal: the planner's answer, else the tree's
        own op. Asked again only for a new goal or state, or after an ask with
        no answer; the reply is taken up once it is back."""
        w = self.world
        for key in lift_changed(self.blocks, w, self.knowledge, self.mem.nav_stuck):
            self.log("planner", f"{key}: block lifted, what it waited on came or its blocker may have changed")
            self.own_blocks.discard(key)
            self.asked_for = None  # the next walk asks afresh
        # A death chest her walk to gave up on is no source the tree picks (A167).
        unreachable = given_up_dests(self.mem.nav_stuck, w.tick, FETCH_GOAL)
        self.turn = turn = walk(w, self.knowledge, self.blocks, self.root, unreachable)
        turn.reading.given_up = tuple(given_up_lines(self.mem, w.tick))  # told the planner (A169)
        if self.goal_since[0] != (turn.goal.key if turn.goal is not None else None):
            self.goal_since, self.last_given_up = (turn.goal.key if turn.goal is not None else None, w.tick), ()
        if turn.goal is None:
            self.asked_for = None
            self.set_op(self.idle_op(turn))  # fills the wait for the answer, never stands in for it
            self.ask_blocked(turn)
            if self.call is not None and self.call.back:
                self.take_reply(turn)
            return self.op
        key = (turn.goal.key, tuple(watch_lines(w, self.knowledge).values()))
        if key != self.asked_for:
            self.asked_for, self.done, self.answered = key, [], False
            self.set_op(self.own_op(turn))
            self.ask(turn)
        elif self.call is None and not self.answered and self.planner.ready():
            self.ask(turn)
        if self.call is not None and self.call.back:
            self.take_reply(turn)
        return self.op

    def own_op(self, turn: Turn) -> GoalOp | None:
        op = turn.op()
        return None if op in self.done else op

    def idle_op(self, turn: Turn) -> GoalOp | None:
        """With no goal while a block stands (A169), she explores, until that is over too."""
        return IDLE_OP if turn.skipped and IDLE_OP not in self.done else None

    def given_up(self) -> tuple[str, ...]:
        """The walks given up for the turn's goal that still hold: those
        given up since it became the goal, and the ones its last op was over
        on (A90, A169). Its block is retried once one of them lifts, which
        takes the ground round it changing, never time alone (A171)."""
        tick, stuck = self.world.tick, self.mem.nav_stuck
        keys = {k for k, g in stuck.given_up.items() if g.since >= self.goal_since[1] and g.holds(tick)}
        keys |= {k for k in self.last_given_up if is_backed_off(stuck, k, tick)}
        return tuple(sorted(keys))

    def shown(self) -> tuple[str, ...]:
        """The give-ups the planner is shown (State ``given_up``,
        ``act.given_up_lines``), still holding: a ``cannot`` it answers may
        rest on one, so the block lapses once one lifts (A171)."""
        stuck, tick = self.mem.nav_stuck, self.world.tick
        return tuple(sorted(k for k, g in stuck.given_up.items() if g.goal.startswith(OWN_WALKS) and g.holds(tick)))

    def block(self, key: str, reason: str) -> None:
        """Block ``key`` for the loop's own reason (a stall, every op over),
        retried as a ``cannot`` is (A169)."""
        self.blocks.mark(key, reason, self.world.tick, seen=seen(self.world, self.knowledge), given_up=self.given_up())
        self.own_blocks.add(key)

    def ask(self, turn: Turn, note: str = "") -> None:
        """Send the question about the turn's goal, unless one is out already,
        with why the last goal was blocked when a stall blocked it (A168)."""
        if self.call is None and turn.goal is not None:
            self.call = self.planner.start(turn.goal, turn.reading, "; ".join(n for n in (self.note, note) if n))
            if self.call is not None:
                self.note = ""

    def blocked_node(self, turn: Turn) -> Node | None:
        """The node to ask about when every way down is blocked: the deepest
        the loop blocked itself (``own_blocks``), else the first a planner's
        ``cannot`` blocked (A169), still in force."""
        active = [n for n, _ in turn.skipped if self.blocks.active(n.key, self.world.tick)]
        return next((n for n in active if n.key in self.own_blocks), active[0] if active else None)

    def ask_blocked(self, turn: Turn) -> None:
        """Every way down is blocked and the world is not beaten (A168, A169):
        the planner is always asked, once per block, about the node
        ``blocked_node`` names, with why, so "no op to work" is never a silent
        hold; exploring only fills the wait for its answer."""
        node = self.blocked_node(turn)
        if node is None or self.call is not None:
            return
        key = (node.key, self.blocks[node.key].until)
        if key == self.asked_blocked:
            return
        block = self.blocks[node.key]
        why = f"every way down is blocked; {node.title}: {block.reason}"
        if block.given_up:
            why += f"; {GIVEN_UP_NOTE}"
        note = "; ".join(n for n in (self.note, why) if n)
        self.call = self.planner.start(node, turn.reading, note)
        if self.call is not None:
            self.asked_blocked, self.note = key, ""

    def take_blocked_reply(self, call: Call, reply: Reply | None) -> None:
        """A reply about the blocked node ``ask_blocked`` asked about: an op
        lifts its block and works it; a ``cannot`` blocks it again with the
        planner's reason; no answer leaves it blocked."""
        node = call.node
        if reply is None:
            return
        self.own_blocks.discard(node.key)
        if reply.op is None:
            answer(node, reply, self.blocks, self.turn.reading, self.given_up(), self.shown())
            self.asked_blocked = (node.key, self.blocks[node.key].until)  # its answer: not asked again for this block
            self.log("planner", f"{node.title}: cannot: {reply.cannot}")
            return
        if self.refused(node, reply.op):
            return
        self.blocks.lift(node.key)
        self.turn = turn = walk(self.world, self.knowledge, self.blocks, self.root)
        turn.reading.given_up = tuple(given_up_lines(self.mem, self.world.tick))
        if turn.goal is None or turn.goal.key != node.key:
            return
        self.asked_for, self.done, self.answered = (node.key, tuple(watch_lines(self.world, self.knowledge).values())), [], True
        self.log("planner", f"{node.title}: {reply.op}")
        self.set_op(reply.op)

    def take_reply(self, turn: Turn) -> None:
        """Take up the reply that is back. One about another goal than the
        turn's is let go: the turn's goal was asked about as it came."""
        call, self.call = self.call, None
        assert call is not None
        reply = self.planner.finish(call)
        goal = turn.goal
        if goal is None and self.asked_blocked is not None and call.node.key == self.asked_blocked[0]:
            self.take_blocked_reply(call, reply)
            return
        if goal is None or call.node.key != goal.key:
            self.met = None
            return
        first, self.met = self.met, None
        self.answered = reply is not None
        if reply is None and self.planner.last_error:
            self.log("planner", f"{goal.title}: {self.planner.last_error}; the tree's op")
            self.planner.last_error = ""
        if first is not None and (reply is None or already_met(reply, turn.reading.world)):
            # Asked once more and no better answer: the first one's plain block, to its expiry.
            self.own_blocks.discard(goal.key)
            answer(goal, first, self.blocks, turn.reading, self.given_up(), self.shown())
            self.set_op(None)
            return
        if reply is None:
            return
        if already_met(reply, turn.reading.world):
            # No wait: one more ask that says so; the tree's op meanwhile.
            self.log("planner", f"{goal.title}: cannot until {reply.until}, already true; asking once more")
            self.met, self.answered = reply, False
            self.ask(turn, ALREADY_MET.format(until=reply.until))
            return
        self.own_blocks.discard(goal.key)
        op = answer(goal, reply, self.blocks, turn.reading, self.given_up(), self.shown())
        if op is None:
            b = self.blocks[goal.key]
            self.log("planner", f"{goal.title}: cannot: {reply.cannot} (until {b.named or 'tick'} {b.was or b.until})")
            self.set_op(None)  # the next walk descends past the blocked node
            return
        if self.refused(goal, op):
            return
        self.log("planner", f"{goal.title}: {op}")
        self.set_op(None if op in self.done else op)

    def refused(self, node: Node, op: GoalOp) -> bool:
        """An answer that walks back to a target her walk gave up and is still
        backed off from is not run (A169, ``act.given_up_target``): the
        tree's op stands, and the next ask says why."""
        why = given_up_target(op, self.ctx())
        if not why:
            return False
        self.log("planner", f"{node.title}: {op} refused: {why}")
        self.note = f"your answer {json.dumps(op, sort_keys=True)} was not run: {why}"
        return True

    def set_op(self, op: GoalOp | None) -> None:
        if op != self.op:
            self.op, self.started = op, None

    def end_op(self, why: str) -> None:
        """The op is over: never run again for this goal, and the tree's op
        stands in. With that one over too, nothing is left to work the goal:
        it is blocked with why, as a ``cannot`` blocks it, to its expiry, so
        the next walk descends past it and asks afresh there."""
        if self.op is not None:
            self.done.append(self.op)
        self.set_op(None)
        goal = self.turn.goal if self.turn is not None else None
        if goal is None:
            return
        self.set_op(self.own_op(self.turn))
        if self.op is None:
            self.block(goal.key, f"every op for it is over: {why}")
            self.log("planner", f"{goal.title}: blocked, every op for it is over ({why})")

    def stalled(self, why: str) -> None:
        """``STALL_ROUNDS`` rounds in a row the op sent nothing (A168): the
        goal is blocked with why, as a ``cannot`` blocks it, so the next walk
        descends past it and asks the planner once, told so."""
        self.stalls, self.empty = 0, None
        goal = self.turn.goal if self.turn is not None else None
        if goal is None:
            return
        reason = f"stalled: {STALL_ROUNDS} rounds with nothing sent ({why})"
        self.block(goal.key, reason)
        self.log("planner", f"{goal.title}: blocked, {reason}")
        self.note = f"{goal.title} was blocked, {reason}"
        self.asked_for = None
        self.set_op(None)

    # Folding a round trip in.

    def apply(self, r: dict, d: Decision) -> list[dict]:
        w, m = self.world, self.mem
        w.tick = int(r.get("tick", w.tick))
        if d.intents is not None:
            # What was sent replaces the server's queue, even an empty one.
            m.pending_intents = d.intents or None
            m.pending_queue = r.get("queue_id") if d.intents else None
            m.pending_next_index, m.queue_sent_tick = 0, w.tick
        self.apply_results(r.get("intent_results") or [])
        earlier = w.entities
        events = w.apply_events(r.get("events_by_tick") or [])
        w.apply_observation(r.get("observation"))
        w.note_level_clear(r.get("level_clear_ceremony"))
        w.learn_threat(events, earlier)
        if any(ev.get("kind") in ("Died", "Respawned") for ev in events):
            self.resync()
        finished = r.get("finished_queue")
        if finished and finished.get("queue_id") == m.pending_queue:
            self.drop_queue()
        elif m.pending_intents is not None and w.tick > m.queue_sent_tick + len(m.pending_intents) + QUEUE_RESULT_SLACK:
            # The queue had time to run out and the server never said so: she may
            # have walked without seeing it.
            self.drop_queue()
            m.need_position, m.path = True, []
        return events

    def apply_results(self, results: list[dict]) -> None:
        """A ``Step`` applied moves her (a door sends her to read position);
        a swing applied starts the weapon's cooldown; a rejection ends the queue."""
        w, m = self.world, self.mem
        for res in sorted(results, key=lambda x: (x.get("tick", 0), x.get("index", 0))):
            if m.pending_intents is None or res.get("queue_id") != m.pending_queue:
                continue
            idx = int(res.get("index", 0))
            if idx < m.pending_next_index or idx >= len(m.pending_intents):
                continue
            intent, tick = m.pending_intents[idx], int(res.get("tick", w.tick))
            if res.get("outcome") == "rejected":
                if intent.get("verb") == "Step" and w.pos is not None:
                    code = (res.get("rejection") or {}).get("code")
                    nav_on_rejection(m, code)
                    learn_step_rejection(m, w, self.knowledge, step_landing(w.pos, intent["direction"]), code, tick)
                self.drop_queue()
                m.need_position, m.path = True, []
                return
            m.pending_next_index = idx + 1
            if intent.get("verb") == "Step" and w.pos is not None:
                w.pos = step_landing(w.pos, intent["direction"])
                m.last_step_tick = tick
                nav_on_step(m, w)
                if w.view.tiles.get(w.pos) in DOORS and w.map_id is not None:
                    # A door moves her: what is queued behind it was planned from here.
                    m.warp_from = (w.map_id, w.pos, w.view.tiles.get(w.pos, "framed_door"))
                    self.drop_queue()
                    m.need_position, m.path = True, []
                    return
            elif is_weapon_swing(intent, w.armed_code) and res.get("outcome") == "applied":
                m.last_use_tick = tick

    def drop_queue(self) -> None:
        m = self.mem
        m.pending_intents, m.pending_queue, m.pending_next_index = None, None, 0
        self.queue_of = None

    def note_warp_landing(self) -> None:
        """After a door step, record where the position read says it landed and
        that she went through that door (``WorldModel.passage``, A144)."""
        w, m, kb = self.world, self.mem, self.knowledge
        warp, m.warp_from = m.warp_from, None
        if warp is None or w.map_id is None or w.pos is None or (w.map_id, w.pos) == warp[:2]:
            return
        from_map, from_pos, block_type = warp
        w.passage = Passage(from_map, from_pos, w.map_id, w.tick, w.pos)
        if kb is not None:
            record_warp(kb, from_map, from_pos, block_type, w.map_id, w.pos)
            note_door_step(m, from_map, from_pos, w.map_id, w.pos)
            settle_entry(kb, w, m)

    def learn_shops(self) -> None:
        """Every supply in sight with a price is a shelf: the cell is where it is
        sold (A131), unless our own ``Drop`` put it there (A158)."""
        w = self.world
        if self.knowledge is None or w.map_id is None:
            return
        for e in w.entities:
            if e.kind == "supply" and e.gem_price is not None and e.gem_price > 0 and not w.is_our_drop(e.id):
                record_shop_cell(self.knowledge, w.map_id, e.pos, e.code or None, e.gem_price)


def op_key(op: GoalOp | None) -> str | None:
    """The op as a key: the queue it sent runs on while it stays the op."""
    return json.dumps(op, sort_keys=True) if op is not None else None
