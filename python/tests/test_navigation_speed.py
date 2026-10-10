"""Navigation stays quick on a big map, and the quick paths change no answer (A23 Run 2, A64).

Run 2 blocked the main thread 5–17 s per decision on a whole-map explore:
``nearest_target`` ran one full A* per unreachable frontier cell, and each
cell priced paid a pass over every hostile, and the held-queue probe ran
the whole decision on every poll. These tests hold the fixes to the old
answers, and guard the time a decision and a probe take on a 30k-cell map.
"""

import contextlib
import random
import statistics
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.navigation import CostGridParams, nearest_target
from agentrealm_agent.navigation.planner import HOSTILE_DANGER, _Grid, _nearest_target_one_by_one, danger_map
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import EXECUTORS, PlayContext
from agentrealm_agent.states.dispatch import PARK_STATES
from agentrealm_agent.states.dispatch import _states as dispatch_states
from agentrealm_agent.threat import type_key_for_entity
from agentrealm_agent.world import Entity, MapView, Tiles, WorldModel

from tests.fixtures.navigation.big_map import RADIUS, START, big_world
from tests.test_runner import FakeClient

BLOCKS = ["dirt"] * 6 + ["grass", "wall", "wall", "lava", "framed_door", None]

# What one decision may take on the big map, at the 95th percentile. The
# target is under 100 ms; the guard leaves room for a slow test machine,
# and still fails long before Run 2's seconds.
DECISION_P95_SECONDS = 0.3
# Cells the cost grid may price in one such decision: a count, so it fails
# the same on any machine. About 17k today; Run 2's code priced millions.
DECISION_CELLS_PRICED = 40_000
# Hostiles scattered over the whole crowded map: ten times the big map's crowd.
CROWD = 400


def random_world(rng: random.Random, size: int = 14) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, perception=4)
    for x in range(size):
        for y in range(size):
            block = rng.choice(BLOCKS)
            if block is not None:
                w.view.tiles[(x, y)] = block
                if block == "lava" and rng.random() < 0.5:
                    w.view.damage[(x, y)] = rng.randint(0, 9)
    w.pos = (rng.randrange(size), rng.randrange(size))
    w.view.tiles[w.pos] = "dirt"
    for i in range(rng.randint(0, 4)):
        kind = rng.choice(["npc", "character", "supply"])
        w.entities.append(Entity(kind, i, (rng.randrange(size), rng.randrange(size))))
    return w


def random_params(rng: random.Random, w: WorldModel) -> CostGridParams:
    cells = list(w.view.tiles)
    kinds = frozenset(rng.choice([["npc"], ["npc", "character"], []]))
    params = CostGridParams(
        avoid=set(rng.sample(cells, rng.randint(0, 6))),
        costly=set(rng.sample(cells, rng.randint(0, 6))),
        is_hostile=lambda w, e: e.kind in kinds,
        allow_goal_door=rng.random() < 0.5,
        fog_cost=rng.choice([2, 2, 4]),
    )
    params.avoid.discard(w.pos)
    if rng.random() < 0.3:
        params.break_costs = {p: rng.randint(2, 20) for p in rng.sample(cells, 3)}
        params.break_nominated = set(params.break_costs) | set(rng.sample(cells, 2))
    if w.entities and rng.random() < 0.5:
        e = rng.choice(w.entities)
        params.danger_peaks = {(e.kind, e.id): rng.choice([0, 10])}
    return params


class NearestTargetMatchesOneByOneTest(unittest.TestCase):
    """One Dijkstra picks the target, and the path, one A* per target did."""

    def test_random_maps(self):
        rng = random.Random(20261006)
        checked = 0
        for _ in range(400):
            w = random_world(rng)
            params = random_params(rng, w)
            known = sorted(w.view.tiles)
            targets = set(rng.sample(known, rng.randint(1, 12)))
            if rng.random() < 0.15:
                targets.add((rng.randint(-5, 20), rng.randint(-5, 20)))  # may lie past the known ground
            if rng.random() < 0.05:
                targets.add(w.pos)
            want = _nearest_target_one_by_one(w, targets, params) if w.pos not in targets else (w.pos, [])
            self.assertEqual(nearest_target(w, targets, params), want, (w.pos, sorted(targets)))
            checked += want is not None
        self.assertGreater(checked, 100)  # most draws reach a target

    def test_no_targets(self):
        w = random_world(random.Random(1))
        self.assertIsNone(nearest_target(w, set(), CostGridParams()))


class DangerMapTest(unittest.TestCase):
    def test_peak_less_five_a_block_out_to_the_radius(self):
        hostiles = [Entity("npc", 0, (0, 0)), Entity("npc", 1, (3, 0)), Entity("npc", 2, (40, 40))]
        table = danger_map(hostiles, {("npc", 2): 12})
        self.assertEqual(table[(0, 0)], HOSTILE_DANGER + HOSTILE_DANGER - 15)
        self.assertEqual(table[(-5, 2)], 5)  # 5 blocks from npc 0, 8 from npc 1
        self.assertNotIn((-6, 0), table)
        self.assertEqual(table[(40, 40)], 12)
        self.assertEqual(table[(42, 40)], 2)
        self.assertNotIn((43, 40), table)
        self.assertEqual(danger_map(hostiles, {("npc", 0): 0, ("npc", 1): 0, ("npc", 2): 0}), {})


class FrontierKeptUpToDateTest(unittest.TestCase):
    """``Tiles.frontier`` after any edit equals a fresh pass."""

    @staticmethod
    def fresh(tiles: dict) -> set:
        return MapView(tiles=dict(tiles)).frontier()

    def test_random_edits(self):
        rng = random.Random(9)
        view = MapView()
        self.assertIsInstance(view.tiles, Tiles)
        for step in range(600):
            p = (rng.randint(0, 15), rng.randint(0, 15))
            op = rng.randrange(8)
            if op < 3:
                view.tiles[p] = rng.choice(["dirt", "grass", "wall", "lava"])
            elif op == 3:
                view.tiles.pop(p, None)
            elif op == 4 and p in view.tiles:
                del view.tiles[p]
            elif op == 5:
                view.tiles.update({(p[0] + i, p[1]): "dirt" for i in range(3)})
            elif op == 6:
                view.tiles.setdefault(p, "wall")
            elif step % 97 == 0:
                view.tiles.clear()
            if rng.random() < 0.4:
                self.assertEqual(view.frontier(), self.fresh(view.tiles), step)
        self.assertEqual(view.frontier(), self.fresh(view.tiles))

    def test_callers_get_their_own_copy(self):
        view = MapView(tiles={(0, 0): "dirt"})
        view.frontier().add((9, 9))
        self.assertEqual(view.frontier(), {(0, 0)})

    def test_a_dict_passed_in_is_wrapped(self):
        view = MapView(tiles={(0, 0): "dirt"})
        self.assertIsInstance(view.tiles, Tiles)
        view.tiles[(5, 5)] = "dirt"
        self.assertEqual(view.frontier(), {(0, 0), (5, 5)})


class BigMapDecisionSpeedTest(unittest.TestCase):
    """A whole-map explore decision on a 30k-cell map stays well under a second."""

    def test_decision_time(self):
        w = big_world()
        self.assertGreater(len(w.view.tiles), 29_000)
        rng = random.Random(1)
        times, priced = [], []
        price = _Grid._price
        for _ in range(20):
            # A fresh decision from somewhere new, after a terrain read: the
            # planner replans from scratch, the worst case.
            while True:
                p = (START[0] + rng.randint(-60, 60), START[1] + rng.randint(-60, 60))
                if w.view.tiles.get(p) in ("grass", "dirt") and all(
                    max(abs(e.pos[0] - p[0]), abs(e.pos[1] - p[1])) > 6 for e in w.entities
                ):
                    break
            w.pos = p
            w.view.tiles[START] = "grass"
            policy = Policy(kind="scripted", goals=["explore"])
            plan = Plan.from_policy(policy, dict(PARAM_DEFAULTS))
            calls = 0

            def counted(grid, p, **kw):
                nonlocal calls
                calls += 1
                return price(grid, p, **kw)

            t0 = time.perf_counter()
            with mock.patch.object(_Grid, "_price", counted):
                d = decide(w, Memory(need_self=False), policy, random.Random(0), plan=plan)
            times.append(time.perf_counter() - t0)
            priced.append(calls)
            self.assertTrue(d.reason.startswith("explore_area"), d.reason)
        self.assertLess(max(priced), DECISION_CELLS_PRICED, priced)
        p95 = statistics.quantiles(times, n=20)[-1]
        self.assertLess(p95, DECISION_P95_SECONDS, f"p95 {p95 * 1000:.0f} ms, median {statistics.median(times) * 1000:.0f} ms")



def crowded_world(rng: random.Random) -> WorldModel:
    """The big map with ``CROWD`` NPCs of a known hostile type on open ground all over it."""
    w = big_world(npcs=0)
    taken = set()
    while len(w.entities) < CROWD:
        p = (START[0] + rng.randint(-RADIUS, RADIUS), START[1] + rng.randint(-RADIUS, RADIUS))
        if w.view.tiles.get(p) in ("grass", "dirt") and p not in taken:
            taken.add(p)
            w.entities.append(Entity(kind="npc", id=2000 + len(w.entities), pos=p, code="fixture_npc"))
    w.hostile_types.add(type_key_for_entity(w.entities[0]))  # every one prices danger and can start a fight
    return w


def stand_somewhere(w: WorldModel, rng: random.Random, *, next_to_hostile: bool) -> None:
    """Put the character on open ground near the middle: beside a hostile, or clear of every one by 6."""
    occupied = {e.pos for e in w.entities}
    while True:
        p = (START[0] + rng.randint(-60, 60), START[1] + rng.randint(-60, 60))
        if w.view.tiles.get(p) not in ("grass", "dirt") or p in occupied:
            continue
        gap = min(max(abs(e.pos[0] - p[0]), abs(e.pos[1] - p[1])) for e in w.entities)
        if (gap == 1) if next_to_hostile else (gap > 6):
            w.pos = p
            return


class CrowdedBigMapDecisionSpeedTest(unittest.TestCase):
    """A64: with hundreds of hostiles on the big map, a decision stays well under a second."""

    def test_decision_time(self):
        rng = random.Random(64)
        w = crowded_world(rng)
        times, states = [], set()
        for i in range(20):
            # Half the decisions stand beside a hostile, so the fight
            # reflexes run their searches; half explore the whole map.
            stand_somewhere(w, rng, next_to_hostile=i % 2 == 0)
            policy = Policy(kind="scripted", goals=["explore"])
            plan = Plan.from_policy(policy, dict(PARAM_DEFAULTS))
            t0 = time.perf_counter()
            d = decide(w, Memory(need_self=False), policy, random.Random(0), plan=plan)
            times.append(time.perf_counter() - t0)
            states.add(d.state)
        self.assertIn("Explore", states)
        self.assertTrue(states - {"Explore"}, "some decisions are reflexes")
        p95 = statistics.quantiles(times, n=20)[-1]
        self.assertLess(p95, DECISION_P95_SECONDS, f"p95 {p95 * 1000:.0f} ms, median {statistics.median(times) * 1000:.0f} ms")


class HeldQueueProbeTest(unittest.TestCase):
    """A64: the held-queue probe runs only the states that can answer with a reflex."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "T.directives.toml").write_text("")

    def test_probe_states(self):
        ctx = PlayContext(Memory(need_self=False), Policy(kind="scripted"), random.Random(0), probe=True)
        reflexes = ["Sync", "Downed", "Escape", "Retreat", "Heal", "Fight", "Flee", "Pickup", "Recover"]
        self.assertEqual([s.name for s in dispatch_states(ctx)], reflexes, "no Greet, no executor")
        ctx.memory.boss = mock.Mock()
        self.assertEqual([s.name for s in dispatch_states(ctx)], reflexes + ["Boss"], "Boss once a boss fight is engaged")
        ctx.memory.parking = True
        self.assertEqual(dispatch_states(ctx), PARK_STATES, "Park walks as a reflex")

    def runner(self, w: WorldModel) -> Runner:
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=["explore"]), self.dir / "T.toml")
        r = Runner(cfg, FakeClient([]), 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.mem.held_queue = "q1"
        return r

    def test_no_executor_runs_and_the_poll_is_quick(self):
        rng = random.Random(7)
        w = crowded_world(rng)
        calls = []
        times = []
        for _ in range(10):
            stand_somewhere(w, rng, next_to_hostile=False)
            r = self.runner(w)
            r.mem.state = "Explore"
            with contextlib.ExitStack() as stack:
                for state in EXECUTORS:
                    stack.enter_context(mock.patch.object(state, "act", side_effect=lambda *a, n=state.name: calls.append(n)))
                t0 = time.perf_counter()
                d = r.reflex_while_held()
                times.append(time.perf_counter() - t0)
            self.assertIsNone(d)
            self.assertEqual(r.mem.state, "Explore", "the state that sent the held queue stands")
        self.assertEqual(calls, [], "no executor ran in the probe")
        self.assertLess(max(times), DECISION_P95_SECONDS, f"max {max(times) * 1000:.0f} ms")

    def test_a_reflex_answers_as_a_full_decision_would(self):
        rng = random.Random(8)
        w = crowded_world(rng)
        fired = 0
        for _ in range(10):
            stand_somewhere(w, rng, next_to_hostile=True)
            policy = Policy(kind="scripted", goals=["explore"])
            plan = Plan.from_policy(policy, dict(PARAM_DEFAULTS))
            want = decide(w, Memory(need_self=False, need_position=False), policy, random.Random(0), plan=plan)
            r = self.runner(w)
            r.rng = random.Random(0)
            got = r.reflex_while_held()
            if want.reflex:
                fired += 1
                self.assertEqual((got.intent, got.reason, got.state), (want.intent, want.reason, want.state))
            else:
                self.assertIsNone(got)
        self.assertGreater(fired, 0)


if __name__ == "__main__":
    unittest.main()
