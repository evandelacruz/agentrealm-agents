"""Starter agent (A52): one readable loop you can copy and grow.

Priority each tick window:
  Sync    — wake or wait until position is known (reads handle the rest)
  Flee    — step away from hostile NPCs in range
  Explore — walk toward map edge (frontier), else a random open step (walk)

The reference agent in ``agentrealm_agent/`` adds dozens of states; this file
keeps the same API contract with only the behaviors above.

Run (from ``python/``, after ``export AGENTREALM_API_KEY=...``):

  python3 starter_agent.py create characters/starter.toml
  python3 starter_agent.py run characters/starter.toml
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field

from agentrealm_agent import config
from agentrealm_agent.client import ApiError, Client
from agentrealm_agent.executor import wait
from agentrealm_agent.world import Pos, WorldModel, chebyshev

SELF_REFRESH = 60
HOSTILE_RANGE = 2
HOSTILE_KINDS = frozenset({"npc"})
AVOID_BLOCKS = frozenset({"fire", "lava"})
WINDOW_MARGIN = 0.05


@dataclass
class StarterMemory:
    path: list[Pos] = field(default_factory=list)
    need_self: bool = True
    need_position: bool = True
    windows_since_self: int = 0
    entity_refresh: int = 5


def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def hostiles_in_range(world: WorldModel) -> list:
    if world.pos is None:
        return []
    here = world.pos
    return [e for e in world.entities if e.kind in HOSTILE_KINDS and chebyshev(e.pos, here) <= HOSTILE_RANGE]


def blocked_tiles(world: WorldModel) -> set[Pos]:
    avoid: set[Pos] = set()
    for p, block in world.view.tiles.items():
        if block in AVOID_BLOCKS:
            avoid.add(p)
    return avoid


def flee_step(world: WorldModel, hostiles: list) -> Pos | None:
    """One step that increases distance from the nearest hostile."""
    here = world.pos
    assert here is not None
    blocked = blocked_tiles(world)
    options = world.open_neighbours(here, blocked) + [here]

    def safety(p: Pos) -> tuple[int, int]:
        nearest = min(chebyshev(p, h.pos) for h in hostiles)
        total = sum(chebyshev(p, h.pos) for h in hostiles)
        return nearest, total

    best = max(options, key=lambda p: (safety(p), p))
    return None if best == here else best


def walk_step(world: WorldModel, mem: StarterMemory, rng: random.Random) -> Pos | None:
    """Follow ``mem.path``, or pick a frontier target and step toward it."""
    if world.pos is None:
        return None
    blocked = blocked_tiles(world)
    if mem.path:
        nxt = mem.path[0]
        if chebyshev(world.pos, nxt) <= world.movement and nxt not in blocked and world.view.walkable(nxt):
            if nxt not in world.occupied():
                mem.path = mem.path[1:]
                return nxt
        mem.path = []

    frontier = world.view.frontier()
    if frontier:
        target = min(frontier, key=lambda p: (chebyshev(p, world.pos), p))
        mem.path = _greedy_path(world, target, blocked)
        if mem.path:
            nxt = mem.path[0]
            mem.path = mem.path[1:]
            return nxt

    options = world.open_neighbours(world.pos, blocked)
    if not options:
        return None
    return rng.choice(sorted(options))


def _greedy_path(world: WorldModel, target: Pos, blocked: set[Pos]) -> list[Pos]:
    """Short list of steps toward ``target`` (not full A* — easy to read)."""
    if world.pos is None:
        return []
    path: list[Pos] = []
    here = world.pos
    for _ in range(8):
        if here == target:
            break
        options = world.open_neighbours(here, blocked)
        if not options:
            break
        here = min(options, key=lambda p: (chebyshev(p, target), p))
        path.append(here)
    return path


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
    if world.tick - world.entities_tick >= mem.entity_refresh:
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

    hostiles = hostiles_in_range(world)
    if hostiles:
        away = flee_step(world, hostiles)
        if away is not None:
            mem.path = []
            return StarterDecision([set_position(away)], "flee hostile", "Flee")
        return StarterDecision(None, "boxed in", "Flee")

    step = walk_step(world, mem, rng)
    if step is not None:
        return StarterDecision([set_position(step)], "explore", "Explore")
    return StarterDecision(None, "nothing to do", "Explore")


def apply_tick(world: WorldModel, mem: StarterMemory, response: dict, sent: list[dict] | None) -> None:
    world.tick = int(response.get("tick", world.tick))
    world.apply_events(response.get("events_by_tick") or [])
    world.apply_observation(response.get("observation"))
    for res in response.get("intent_results") or []:
        if res.get("outcome") == "rejected":
            mem.need_position = True
            mem.path = []
            break


class StarterRunner:
    def __init__(self, cfg: config.CharacterConfig, client: Client, cid: int, stop: threading.Event, out=print):
        self.cfg = cfg
        self.client = client
        self.cid = cid
        self.stop = stop
        self.out = out
        self.world = WorldModel(cid)
        self.mem = StarterMemory(entity_refresh=cfg.policy.entity_refresh)
        seed = cfg.policy.seed if cfg.policy.seed is not None else cid
        self.rng = random.Random(seed)
        cfg.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace = open(cfg.trace_path, "a", buffering=1)

    def log(self, call: str, detail: str) -> None:
        w = self.world
        pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos else "?"
        self.out(f"[{self.cfg.name}] t={w.tick} @{pos} {call:<8} {detail}")
        self.trace.write(
            json.dumps({"tick": w.tick, "pos": w.pos, "map": w.map_id, "call": call, "detail": detail}) + "\n"
        )

    def run(self) -> None:
        hz = 1
        try:
            world_body = self.client.world(self.cid)
            hz = max(1, int(world_body.get("tick_rate_hz", 1)))
            self.log("world", f"{world_body.get('code')} {hz}Hz")
        except ApiError as e:
            self.out(f"[{self.cfg.name}] world read failed: {e}")
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
                    self.out(f"[{self.cfg.name}] {call}: {e}")
                    if e.retry_after:
                        time.sleep(e.retry_after)
        finally:
            self.trace.close()

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
            apply_tick(w, m, r, intents)
            label = d.mode if intents else "hold"
            self.log("tick", f"{label}: {d.reason}")


def create(client: Client, cfgs: list[config.CharacterConfig]) -> int:
    failed = 0
    for cfg in cfgs:
        if (state := config.load_state(cfg)) is not None:
            print(f"{cfg.name}: already created as {state['character_id']}")
            continue
        try:
            s = client.create_character(cfg.world, cfg.name, cfg.avatar, cfg.model_agent)
        except ApiError as e:
            print(f"{cfg.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        config.save_state(cfg, {"character_id": s["id"], "world": cfg.world})
        print(f"{cfg.name}: created {s['id']} in {cfg.world}")
    return 1 if failed else 0


def run(client: Client, cfgs: list[config.CharacterConfig]) -> int:
    ids: list[tuple[config.CharacterConfig, int]] = []
    for cfg in cfgs:
        state = config.load_state(cfg)
        if state is None:
            print(f"{cfg.name}: not created yet; run `create` first", file=sys.stderr)
            return 2
        ids.append((cfg, int(state["character_id"])))
    stop = threading.Event()
    threads = [
        threading.Thread(target=lambda cfg=cfg, cid=cid: StarterRunner(cfg, client, cid, stop).run(), daemon=True)
        for cfg, cid in ids
    ]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        print("stopping")
    finally:
        stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="starter_agent", description="Minimal Agent Realm loop (A52).")
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg"))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create", "run"):
        p = sub.add_parser(name)
        p.add_argument("characters", nargs="+", help="character .toml files")
    args = ap.parse_args(argv)
    try:
        cfgs = [config.load(p) for p in args.characters]
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    client = Client(args.base_url, args.api_key)
    return {"create": create, "run": run}[args.cmd](client, cfgs)


if __name__ == "__main__":
    sys.exit(main())
