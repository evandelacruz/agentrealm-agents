"""Starter agent (A52): one readable loop you can copy and grow.

Priority each tick window:
  Sync    — wake or wait until position is known (reads handle the rest)
  Flee    — step away from hostile NPCs in range
  Explore — walk to the nearest frontier tile (a known tile next to unknown
            ones), else take a random open step

It moves with one ``SetPosition`` per tick POST, to a neighbouring tile. A new
move goes out only after the last one's result is back and the movement
cooldown has passed (ticks per move at ``movement_speed``), and its position
advances when that result says ``applied``. The reference agent instead sends
paced ``Step``/``Wait`` queues (PLAN.md **Executor**).

The reference agent in ``agentrealm_agent/`` adds dozens of states; this file
keeps the same API contract with only the behaviors above.

From the character file's ``[policy]`` it reads only ``hostile``,
``hostile_range``, ``avoid_blocks``, ``entity_refresh``, and ``seed``. Other
keys (``goals``, ``goto``, ``on_hostile``, ``pickup``) are ignored here.

On 401/403 it stops with a message. After a death, or while unplaced, it drops
its path and rereads self and position.

Run (from ``python/``, after ``export AGENTREALM_API_KEY=...``):

  python3 starter_agent.py create characters/starter.toml
  python3 starter_agent.py run characters/starter.toml --character-id ID
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from agentrealm_agent import config
from agentrealm_agent.character_select import (
    DEFAULT_CREATE_AVATAR,
    DEFAULT_CREATE_NAME,
    CharacterSelectionError,
    create,
    resolve_character_id,
)
from agentrealm_agent.client import ApiError, Client
from agentrealm_agent.executor import wait
from agentrealm_agent.executor.movement import ticks_per_step
from agentrealm_agent.pathing import flee_step, hostiles_in_range
from agentrealm_agent.world import Pos, WorldModel, chebyshev

SELF_REFRESH = 60
WINDOW_MARGIN = 0.05
MOVE_RESULT_TIMEOUT = 5  # ticks to wait for a move's result before rereading position


@dataclass
class StarterMemory:
    path: list[Pos] = field(default_factory=list)
    need_self: bool = True
    need_position: bool = True
    windows_since_self: int = 0
    policy: config.Policy = field(default_factory=config.Policy)
    hz: int = 1  # world tick rate, from GET world
    server_tick: int = 0  # latest tick a tick response reported
    move: Pos | None = None  # SetPosition sent; its result is not back yet
    move_queue: str | None = None  # queue_id the server gave that move
    move_sent_tick: int = 0
    last_move_tick: int | None = None  # tick our last move applied


def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def move_ready(world: WorldModel, mem: StarterMemory) -> bool:
    """True when a move now would not be rejected with ``movement_cooldown``."""
    if mem.move is not None:
        return False
    if mem.last_move_tick is None:
        return True
    wait_ticks = ticks_per_step(tick_rate_hz=mem.hz, movement_speed_milli=world.movement_speed)
    return mem.server_tick - mem.last_move_tick >= wait_ticks


def blocked_tiles(world: WorldModel, policy: config.Policy) -> set[Pos]:
    return {p for p, block in world.view.tiles.items() if block in policy.avoid_blocks}


def walk_step(world: WorldModel, mem: StarterMemory, rng: random.Random) -> Pos | None:
    """Follow ``mem.path``, or plan a new one to the nearest frontier tile."""
    if world.pos is None:
        return None
    blocked = blocked_tiles(world, mem.policy)
    if mem.path:
        nxt = mem.path[0]
        if chebyshev(world.pos, nxt) <= world.movement and nxt not in blocked and world.view.walkable(nxt):
            if nxt not in world.occupied():
                mem.path = mem.path[1:]
                return nxt
        mem.path = []

    mem.path = _path_to_frontier(world, blocked)
    if mem.path:
        nxt = mem.path[0]
        mem.path = mem.path[1:]
        return nxt

    options = world.open_neighbours(world.pos, blocked)
    if not options:
        return None
    return rng.choice(sorted(options))


def _path_to_frontier(world: WorldModel, blocked: set[Pos]) -> list[Pos]:
    """Steps to the nearest reachable frontier tile, by breadth-first search
    over known walkable tiles, so walls never trap it the way a greedy step can."""
    start = world.pos
    frontier = world.view.frontier() - {start}
    came_from: dict[Pos, Pos | None] = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        if here in frontier:
            path = []
            while here != start:
                path.append(here)
                here = came_from[here]
            return path[::-1]
        for n in sorted(world.open_neighbours(here, blocked)):
            if n not in came_from:
                came_from[n] = here
                queue.append(n)
    return []


def choose_call(world: WorldModel, mem: StarterMemory) -> str:
    """Which API call fits this window (one per tick)."""
    if mem.need_self or mem.windows_since_self >= SELF_REFRESH:
        return "self"
    if mem.need_position or world.pos is None:
        if world.asleep:
            return "tick"
        return "position"
    if world.terrain_stale():
        return "terrain"
    if world.tick - world.entities_tick >= mem.policy.entity_refresh:
        return "entities"
    return "tick"


@dataclass
class StarterDecision:
    intents: list[dict] | None
    reason: str
    mode: str  # Sync | Flee | Explore


def decide(world: WorldModel, mem: StarterMemory, rng: random.Random) -> StarterDecision:
    """Pick Sync, Flee, or Explore for this tick POST."""
    if world.asleep:
        mem.need_self = True
        return StarterDecision([wait()], "wake", "Sync")

    hostiles = hostiles_in_range(world, mem.policy)
    if not move_ready(world, mem):
        return StarterDecision(None, "move cooldown", "Flee" if hostiles else "Explore")
    if hostiles:
        away = flee_step(world, hostiles, blocked_tiles(world, mem.policy))
        if away is not None:
            mem.path = []
            return StarterDecision([set_position(away)], "flee hostile", "Flee")
        return StarterDecision(None, "boxed in", "Flee")

    step = walk_step(world, mem, rng)
    if step is not None:
        return StarterDecision([set_position(step)], "explore", "Explore")
    return StarterDecision(None, "nothing to do", "Explore")


def resync(mem: StarterMemory) -> None:
    """Forget the plan and reread self and position (death, respawn, unplaced)."""
    mem.need_self = mem.need_position = True
    mem.path = []
    mem.move = None


def apply_tick(world: WorldModel, mem: StarterMemory, response: dict, sent: list[dict] | None = None) -> None:
    """Fold one tick response; ``sent`` is the intents that POST carried."""
    world.tick = mem.server_tick = int(response.get("tick", world.tick))
    events = world.apply_events(response.get("events_by_tick") or [])
    world.apply_observation(response.get("observation"))
    if any(ev.get("kind") in ("Died", "Respawned") for ev in events):
        resync(mem)
    if sent and sent[0]["verb"] == "SetPosition":
        mem.move = (sent[0]["x"], sent[0]["y"])
        mem.move_queue, mem.move_sent_tick = response.get("queue_id"), mem.server_tick
    for res in response.get("intent_results") or []:
        if res.get("outcome") == "rejected":
            mem.need_position = True
            mem.path = []
            mem.move = None
            break
        if mem.move is not None and res.get("queue_id") == mem.move_queue:
            # The tick response comes back before our intent runs, so the
            # position moves when the result arrives, not when we send.
            if res.get("outcome") == "applied":
                world.pos = mem.move
            mem.last_move_tick = int(res.get("tick", mem.server_tick))
            mem.move = None
    if mem.move is not None and mem.server_tick - mem.move_sent_tick > MOVE_RESULT_TIMEOUT:
        mem.need_position, mem.path, mem.move = True, [], None  # result lost; we may have moved


class StarterRunner:
    def __init__(self, cfg: config.CharacterConfig, client: Client, cid: int, stop: threading.Event, out=print):
        self.cfg = cfg
        self.client = client
        self.cid = cid
        self.stop = stop
        self.out = out
        self.failed = False  # set when it stopped on an error, not on Ctrl-C
        self.world = WorldModel(cid)
        self.mem = StarterMemory(policy=cfg.policy)
        seed = cfg.policy.seed if cfg.policy.seed is not None else cid
        self.rng = random.Random(seed)
        trace = cfg.trace_path(cid)
        trace.parent.mkdir(parents=True, exist_ok=True)
        self.trace = open(trace, "a", buffering=1)

    def log(self, call: str, detail: str) -> None:
        w = self.world
        pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos else "?"
        self.out(f"[{self.cfg.profile}] t={w.tick} @{pos} {call:<8} {detail}")
        self.trace.write(
            json.dumps({"tick": w.tick, "pos": w.pos, "map": w.map_id, "call": call, "detail": detail}) + "\n"
        )

    def run(self) -> None:
        hz = 1
        try:
            world_body = self.client.world(self.cid)
            hz = max(1, int(world_body.get("tick_rate_hz", 1)))
            self.mem.hz = hz
            self.log("world", f"{world_body.get('code')} {hz}Hz")
        except ApiError as e:
            self.out(f"[{self.cfg.profile}] world read failed: {e}")
            self.failed = True
            self.trace.close()
            return

        window = 1.0 / hz
        try:
            while not self.stop.is_set():
                now = time.time()
                next_open = (int(now / window) + 1) * window + WINDOW_MARGIN
                time.sleep(max(0.0, next_open - now))
                self.world.tick += 1
                self.mem.windows_since_self += 1
                call = choose_call(self.world, self.mem)
                try:
                    self.step(call)
                except ApiError as e:
                    if not self.on_error(call, e):
                        self.failed = True
                        return
        finally:
            self.trace.close()

    def on_error(self, call: str, e: ApiError) -> bool:
        """Log an API error and react; False means stop this character."""
        self.out(f"[{self.cfg.profile}] {call}: {e}")
        if e.status in (401, 403):
            self.out(f"[{self.cfg.profile}] stopping: API key rejected or not allowed to play this character")
            return False
        if e.code in ("not_on_map", "character_not_live"):
            resync(self.mem)  # dead, respawning, or not placed yet
        self.stop.wait(e.retry_after or 0)
        return True

    def step(self, call: str) -> None:
        w, m, c = self.world, self.mem, self.client
        if call == "self":
            s = c.self_(self.cid)
            w.apply_self(s)
            m.need_self, m.windows_since_self = False, 0
            self.log(call, f"alive={w.alive} perception={w.perception}")
        elif call == "position":
            p = c.position(self.cid)
            w.apply_position(p)
            m.need_position, m.path = False, []
            self.log(call, "")
        elif call == "terrain":
            t = c.terrain(self.cid, w.map_id, *w.perception_rect())
            w.apply_terrain(t)
            self.log(call, f"{len(w.view.tiles)} known tiles")
        elif call == "entities":
            e = c.entities(self.cid, w.map_id, *w.perception_rect())
            w.apply_entities(e)
            self.log(call, f"{len(w.entities)} entities")
        else:
            d = decide(w, m, self.rng)
            intents = d.intents
            r = c.tick(self.cid, intents, snapshot_version=w.snapshot_version)
            apply_tick(w, m, r, sent=intents)
            label = d.mode if intents else "hold"
            self.log("tick", f"{label}: {d.reason}")


# Starter characters walk with the wander model agent (A52); name and avatar
# defaults, and `create` itself, are shared with the reference runner.
STARTER_CREATE_MODEL = "agentrealm-reference/wander"


def run(client: Client, cfg: config.CharacterConfig, cid: int) -> int:
    try:
        client.self_(cid)
    except ApiError as e:
        print(f"{cfg.profile} ({cid}): {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    runner = StarterRunner(cfg, client, cid, stop)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        print("stopping")
    finally:
        stop.set()
    return 1 if runner.failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="starter_agent", description="Minimal Agent Realm loop (A52).")
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg"))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    create_p = sub.add_parser("create")
    create_p.add_argument("profile", help="behavior profile .toml file")
    create_p.add_argument("--name", default=DEFAULT_CREATE_NAME)
    create_p.add_argument("--avatar", default=DEFAULT_CREATE_AVATAR)
    create_p.add_argument("--model-agent", default=STARTER_CREATE_MODEL)
    run_p = sub.add_parser("run")
    run_p.add_argument("profile", help="behavior profile .toml file")
    run_p.add_argument("--character-id", type=int, default=None)
    run_p.add_argument("--character-name", default=None)
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.profile)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    client = Client(args.base_url, args.api_key)
    if args.cmd == "create":
        return create(
            client,
            cfg,
            name=args.name.strip(),
            avatar=args.avatar.strip(),
            model_agent=args.model_agent.strip(),
        )
    try:
        cid = resolve_character_id(
            client,
            cfg,
            character_id=args.character_id,
            character_name=args.character_name,
        )
    except CharacterSelectionError as e:
        print(e, file=sys.stderr)
        return 2
    return run(client, cfg, cid)


if __name__ == "__main__":
    sys.exit(main())
