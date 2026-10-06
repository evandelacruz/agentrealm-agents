"""A16: M7 acceptance metrics, navigation fixture, and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.acceptance import AcceptanceHooks
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.acceptance_survival import LOOP_STEP_LIMIT, OSCILLATION_ABORT_COUNT, OSCILLATION_ABORT_TICKS
from agentrealm_agent.m7_acceptance import TARGET_DISTANCE, M7AcceptanceMetrics
from agentrealm_agent.memory import Memory
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone
from tests.fixtures.navigation import grids, sim
from tests.test_m6_acceptance import FakeMovementServer, RunnerCase

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m7_olympuff.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m7_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OVERWORLD = 7


def metrics(**kw) -> M7AcceptanceMetrics:
    kw.setdefault("overworld_map_id", OVERWORLD)
    kw.setdefault("origin", (0, 0))
    kw.setdefault("target", (TARGET_DISTANCE, 0))
    return M7AcceptanceMetrics(**kw)


def open_world(pos=(0, 0)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=OVERWORLD, pos=pos, perception=5, tick=1)
    for y in range(-3, 4):
        for x in range(-3, 6):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = pos, OVERWORLD
    return w


STEP = [{"verb": "Step", "direction": "right"}]


def decide(m, w, mem=None, *, state="Explore", reason="explore", intents=STEP, knowledge=None, plan_op=None):
    """One ``before_tick`` call; ``intents=None`` is a held queue, nothing new sent."""
    m.before_tick(
        w,
        mem or Memory(),
        state=state,
        reason=reason,
        intents=intents,
        policy=Policy(hostile=["npc"]),
        params=dict(PARAM_DEFAULTS),
        knowledge=knowledge,
        plan_op=plan_op,
    )


def give_up_signal(target, *, map_id=OVERWORLD, tick=5, goal="plan_travel", reason="no_path"):
    return {
        "trigger": "stuck",
        "reason": reason,
        "escalation": [reason, "no_frontier"],
        "goal": goal,
        "goal_key": f"{goal}:{map_id}:{target[0]},{target[1]}",
        "target": list(target),
        "map_id": map_id,
        "tick": tick,
    }


class NavigationGateTest(unittest.TestCase):
    def test_fails_until_target_reached_or_given_up(self):
        m = metrics()
        self.assertFalse(m.navigation_ok())
        self.assertTrue(any("neither reached nor given up" in f for f in m.failures()))

    def test_reaching_the_target_passes(self):
        m = metrics(target=(2, 0))
        decide(m, open_world((1, 0)))
        self.assertFalse(m.navigation_ok())
        self.assertEqual(m.max_distance, 1)
        decide(m, open_world((2, 0)))
        self.assertTrue(m.navigation_ok())

    def test_far_from_origin_without_the_target_does_not_pass(self):
        m = metrics()
        decide(m, open_world((-TARGET_DISTANCE, 0)))
        self.assertEqual(m.max_distance, TARGET_DISTANCE)
        self.assertFalse(m.navigation_ok())

    def test_give_up_on_the_target_passes_with_its_reason(self):
        m, mem = metrics(), Memory()
        mem.nav_stuck.stuck_signals.append(give_up_signal((TARGET_DISTANCE, 0), reason="no_break"))
        decide(m, open_world(), mem)
        self.assertTrue(m.navigation_ok())
        self.assertEqual(m.target_give_up, "no_break", "the reason string, not the escalation list")

    def test_give_up_on_another_goal_is_counted_but_never_passes(self):
        m, mem = metrics(), Memory()
        mem.nav_stuck.stuck_signals.append(give_up_signal((4, 4), goal="explore_area"))
        mem.nav_stuck.stuck_signals.append(give_up_signal((TARGET_DISTANCE, 0), map_id=99))
        decide(m, open_world(), mem)
        decide(m, open_world(), mem)  # the same signals, read again before a drain
        self.assertFalse(m.navigation_ok())
        self.assertEqual(m.other_give_ups, 2)


class SurvivalGateTest(unittest.TestCase):
    def threatened(self) -> WorldModel:
        w = open_world()
        w.health, w.lives = 3, 6
        w.entities = [Entity("npc", 1, (1, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), 5)
        return w

    def test_retreat_miss_when_the_decision_saw_the_threat_and_did_not_retreat(self):
        m = metrics()
        decide(m, self.threatened(), state="Explore")
        self.assertEqual(m.retreat_misses, 1)
        self.assertTrue(any("should_retreat" in f for f in m.failures()))

    def test_no_retreat_miss_from_a_survival_state(self):
        m = metrics()
        for state in ("Retreat", "Flee", "Heal"):
            decide(m, self.threatened(), state=state)
        self.assertEqual(m.retreat_misses, 0)

    def recover(self, m, w, intents):
        w.death_chest = (OVERWORLD, (3, 0), 11)
        decide(m, w, state="Recover", reason="recover chest", intents=intents)

    def test_withdraw_from_a_cell_not_known_safe_fails(self):
        m, w = metrics(), open_world((2, 0))
        apply_zone(w, OVERWORLD, 3, 0, {"safe": True})  # the chest tile is safe, where we stand is not
        self.recover(m, w, [{"verb": "WithdrawFromChest", "chest_id": 11}])
        self.assertEqual((m.recover_withdraws, m.recover_unsafe), (1, 1))
        self.assertTrue(any("not known safe" in f for f in m.failures(full_hour=False)))

    def test_withdraw_on_a_known_safe_tile_passes(self):
        m, w = metrics(), open_world((2, 0))
        apply_zone(w, OVERWORLD, 2, 0, {"safe": True})
        self.recover(m, w, [{"verb": "WithdrawFromChest", "chest_id": 11}])
        self.assertEqual((m.recover_withdraws, m.recover_unsafe), (1, 0))

    def test_withdraw_is_judged_where_the_queue_puts_the_agent(self):
        m, w = metrics(), open_world((1, 0))
        apply_zone(w, OVERWORLD, 1, 0, {"safe": True})  # safe where we stand, not where we withdraw
        self.recover(m, w, [{"verb": "Step", "direction": "right"}, {"verb": "WithdrawFromChest", "chest_id": 11}])
        self.assertEqual((m.recover_withdraws, m.recover_unsafe), (1, 1))

    def test_steps_toward_the_chest_are_not_withdraws(self):
        m, w = metrics(), open_world((0, 0))
        self.recover(m, w, [{"verb": "SetPosition", "x": 1, "y": 0}])
        self.assertEqual((m.recover_withdraws, m.recover_unsafe), (0, 0))

    def test_regen_must_be_measured_on_a_full_hour(self):
        m = metrics(target=(0, 0))
        decide(m, open_world())
        self.assertIn("safe-zone regen never measured", m.failures())
        self.assertNotIn("safe-zone regen never measured", m.failures(full_hour=False))
        mem = Memory()
        mem.heal_regen_absent = True
        decide(m, open_world(), mem)
        self.assertEqual(m.regen, "no")
        self.assertEqual(m.failures(), [])

    def test_a_death_fails_the_run(self):
        m = metrics()
        m.on_death()
        self.assertIn("1 death(s) during run", m.failures(full_hour=False))


class LoopGateTest(unittest.TestCase):
    def test_steps_at_one_cell_with_one_reason_are_a_loop(self):
        m = metrics()
        for _ in range(LOOP_STEP_LIMIT):
            decide(m, open_world(), reason="explore → (1,0)")
        self.assertTrue(m.loop_detected)
        self.assertTrue(any("loop" in f for f in m.failures(full_hour=False)))

    def test_heal_resting_in_place_is_not_a_loop(self):
        m = metrics()
        for _ in range(LOOP_STEP_LIMIT * 3):
            decide(m, open_world(), state="Heal", reason="rest in safe zone", intents=[{"verb": "Wait"}])
        self.assertFalse(m.loop_detected)

    def test_a_held_queue_neither_counts_nor_resets(self):
        m = metrics()
        for _ in range(LOOP_STEP_LIMIT):
            decide(m, open_world(), reason="explore → (1,0)")
            decide(m, open_world(), reason="queue held", intents=None)
        self.assertTrue(m.loop_detected)


def gave_up(tick: int) -> dict:
    """A guard event that gave up the goto."""
    return {"tick": tick, "cells": [[1, 0], [2, 0]], "states": ["Break", "Explore"], "goal": "goto", "target": [150, 0]}


def kept(tick: int) -> dict:
    """A guard event where survival states did the moving and nothing was given up."""
    return {"tick": tick, "cells": [[1, 0], [2, 0]], "states": ["Fight", "Retreat"]}


class OscillationAbortTest(unittest.TestCase):
    def test_sustained_give_ups_stop_the_run_and_fail_it(self):
        stop = threading.Event()
        m = metrics(stop=stop)
        for i in range(OSCILLATION_ABORT_COUNT):
            m.on_oscillation(gave_up(i * 100))
        self.assertFalse(stop.is_set(), "a few give-ups are the guard doing its job")
        m.on_oscillation(gave_up(OSCILLATION_ABORT_COUNT * 100))
        self.assertTrue(stop.is_set())
        self.assertTrue(any("sustained oscillation" in f for f in m.failures(full_hour=False)))
        self.assertIn("goto", m.oscillation_abort)

    def test_survival_only_pacing_never_aborts(self):
        stop = threading.Event()
        m = metrics(stop=stop)
        for i in range(OSCILLATION_ABORT_COUNT * 10):
            m.on_oscillation(kept(i * 10))
        self.assertFalse(stop.is_set())
        self.assertEqual(m.failures(full_hour=False), [])
        self.assertEqual(len(m.oscillation_ticks), OSCILLATION_ABORT_COUNT * 10, "still counted and reported")

    def test_events_spread_past_the_window_do_not_abort(self):
        stop = threading.Event()
        m = metrics(stop=stop)
        for i in range(OSCILLATION_ABORT_COUNT * 3):
            m.on_oscillation(gave_up(i * OSCILLATION_ABORT_TICKS // 2))
        self.assertFalse(stop.is_set())
        self.assertEqual(m.failures(full_hour=False), [])


class ApiErrorTest(unittest.TestCase):
    def test_api_errors_fail_the_run(self):
        class Reads:
            def tick(self, *a, **k):
                raise ApiError(429, "rate_limited")

        m = metrics()
        client = m.wrap(Reads())
        with self.assertRaises(ApiError):
            client.tick(1, None)
        self.assertTrue(any("API error" in f for f in m.failures(full_hour=False)))


class TownServer(FakeMovementServer):
    """The M6 fake server, with a town on map 7 so the runner knows the overworld."""

    def world(self, cid):
        return {**super().world(cid), "town": {"map_id": OVERWORLD, "x": 0, "y": 0}}


class RunnerHookTest(RunnerCase):
    def test_walks_to_the_target_and_stops_when_the_clock_runs_out(self):
        stop = threading.Event()
        ticks = iter(range(10**6))
        m = metrics(target=(80, 0), target_seconds=600, stop=stop, clock=lambda: float(next(ticks)))
        server = TownServer(5000, stop)
        self.run_against(server, self.make_runner(server, stop, m))
        self.assertTrue(stop.is_set())
        self.assertGreater(server.windows, 0, "stopped by the clock, not when the windows ran out")
        self.assertTrue(m.target_reached)
        self.assertEqual(m.max_distance, 80)
        self.assertFalse(m.loop_detected)
        self.assertEqual(m.failures(full_hour=False), [])

    def test_hooks_see_the_world_before_the_tick_response(self):
        calls: list[str] = []

        class Hooks(AcceptanceHooks):
            def before_tick(self, w, m, **kw):
                calls.append(f"before_tick health={w.health}")

        class Server(TownServer):
            def tick(self, cid, intents, *, snapshot_version=None):
                calls.append("tick")
                r = super().tick(cid, intents, snapshot_version=snapshot_version)
                r["observation"] = {"health": 1}
                return r

        stop = threading.Event()
        server = Server(10, stop)
        r = self.make_runner(server, stop, Hooks())
        r.world.health = 10
        r.tick()
        self.assertEqual(calls, ["before_tick health=10", "tick"])

    def test_oscillation_events_reach_the_trace_and_the_gate(self):
        stop = threading.Event()
        m = metrics()
        server = TownServer(10, stop)
        r = self.make_runner(server, stop, m)
        event = {"event": "oscillation", "tick": 5, "map_id": OVERWORLD, "cells": [[1, 0], [2, 0]]}
        r.mem.nav_stuck.oscillations.append(event)
        with mock.patch.object(r, "log") as log:
            r.trace_oscillations()
        log.assert_called_once()
        self.assertEqual(log.call_args.args[0], "oscillation")
        self.assertEqual(m.oscillation_ticks, [5])
        self.assertEqual(r.mem.nav_stuck.oscillations, [])

    def test_died_event_reaches_on_death(self):
        stop = threading.Event()
        m = metrics()
        server = TownServer(10, stop)
        r = self.make_runner(server, stop, m)
        m.stop = stop
        r.on_events([{"kind": "Died", "cause": "killed"}])
        self.assertEqual(m.deaths, 1)
        self.assertTrue(stop.is_set(), "a death ends the run")


class NavigationFixtureTest(unittest.TestCase):
    def test_open_corridor_reaches_150_blocks(self):
        sc = grids.OPEN_CORRIDOR_150
        policy = sim.scripted(goals=["goto"], goto=sc.goal)
        r = sim.run(sc, policy, max_decisions=500)
        self.assertEqual(r.outcome, "reached")
        dist = abs(r.world.pos[0] - sc.start[0])
        self.assertGreaterEqual(dist, TARGET_DISTANCE)

    def test_hedge_gives_up_with_reason_not_loop(self):
        sc = grids.HEDGE_LINE
        policy = sim.scripted(goals=["goto"], goto=sc.goal)
        r = sim.run(sc, policy)
        self.assertEqual(r.outcome, "abandoned")
        self.assertTrue(r.memory.nav_stuck.stuck_signals)
        self.assertIn("escalation", r.signal or {})


class FakeSleeper:
    """A client whose character starts asleep (placed: false) and wakes on an intent."""

    def __init__(self, *, asleep=True, wakes=True, refusal=None, downed_reads=0):
        self.asleep, self.wakes, self.refusal = asleep, wakes, refusal
        self.downed_reads = downed_reads
        self.ticks: list = []
        self.on_position = lambda: None

    def self_(self, cid):
        if self.downed_reads:
            self.downed_reads -= 1
            return {"alive": False}
        return {"alive": True, "asleep": self.asleep}

    def tick(self, cid, intents=None):
        self.ticks.append(intents)
        if self.refusal:
            return {"intent_results": [{"outcome": "rejected", "rejection": {"code": self.refusal}}]}
        if self.wakes:
            self.asleep = False
        return {"asleep": self.asleep}

    def world(self, cid):
        return {"town": {"map_id": OVERWORLD, "x": 0, "y": 0}}

    def position(self, cid):
        if self.asleep:
            raise ApiError(409, "not_on_map")
        self.on_position()
        return {"map_id": OVERWORLD, "x": 10, "y": 20}


class SmokeScriptTest(unittest.TestCase):
    def setUp(self):
        self.smoke = load_smoke()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patch = mock.patch.object(config, "STATE_DIR", self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env or {}, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = self.smoke.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_api_key_exits_2(self):
        code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def run_main(self, seconds: float, played):
        """``main`` against a fake client; ``played(metrics)`` stands in for the hour."""

        def run_smoke(client, cfg, cid, metrics, *, timeout_s, out=None):
            played(metrics)
            return seconds, None

        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "run_acceptance_smoke", side_effect=run_smoke):
            client = Client.return_value
            client.world.return_value = {"town": {"map_id": OVERWORLD, "x": 0, "y": 0}}
            client.position.return_value = {"map_id": OVERWORLD, "x": 10, "y": 20}
            client.self_.return_value = {"alive": True}
            return self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(seconds)])

    def test_target_is_150_east_of_the_start(self):
        seen = []
        self.run_main(10, seen.append)
        self.assertEqual((seen[0].origin, seen[0].target), ((10, 20), (10 + TARGET_DISTANCE, 20)))

    def test_short_run_passes_without_navigation(self):
        code, out, _ = self.run_main(10, lambda m: None)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_full_hour_fails_without_navigation_or_regen(self):
        code, _, err = self.run_main(3600, lambda m: None)
        self.assertEqual(code, 1)
        self.assertIn("neither reached nor given up", err)
        self.assertIn("regen never measured", err)

    def test_full_hour_passes_on_a_give_up_with_a_reason(self):
        def played(m):
            m.target_give_up, m.regen = "no_path", "yes"

        code, out, _ = self.run_main(3600, played)
        self.assertEqual(code, 0, out)
        self.assertIn("gave up (no_path)", out)

    def test_sustained_oscillation_exits_1_with_a_clear_message(self):
        def played(m):
            for i in range(OSCILLATION_ABORT_COUNT + 1):
                m.on_oscillation(gave_up(i))

        code, _, err = self.run_main(3600, played)
        self.assertEqual(code, 1)
        self.assertIn("ABORT: sustained oscillation", err)

    def test_start_off_the_overworld_exits_2(self):
        client = mock.Mock()
        client.world.return_value = {"town": {"map_id": OVERWORLD}}
        client.position.return_value = {"map_id": 12, "x": 0, "y": 0}
        with self.assertRaises(ValueError):
            self.smoke.navigation_start(client, 9)

    def test_start_wakes_a_sleeping_character_before_reading_position(self):
        client = FakeSleeper()
        self.smoke.wake(client, 9, pause=lambda s: None)
        self.assertEqual(client.ticks, [[{"verb": "Wait"}]], "one Wait wakes it")
        self.assertEqual(client.self_(9), {"alive": True, "asleep": False})

    def test_start_waits_out_a_respawn(self):
        client = FakeSleeper(asleep=False, downed_reads=2)
        self.smoke.wake(client, 9, pause=lambda s: None)
        self.assertEqual(client.ticks, [], "nothing sent while downed")

    def test_start_exits_with_the_wake_refusal(self):
        for code in ("alive_cap_full", "block_occupied", "something_new"):
            client = FakeSleeper(refusal=code)
            with self.assertRaisesRegex(ValueError, code):
                self.smoke.wake(client, 9, pause=lambda s: None)

    def test_start_gives_up_on_a_character_that_never_wakes(self):
        client = FakeSleeper(wakes=False)
        with self.assertRaisesRegex(ValueError, "still asleep"):
            self.smoke.wake(client, 9, pause=lambda s: None)

    def test_main_wakes_then_reads_position(self):
        seen = []
        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(
                    self.smoke,
                    "run_acceptance_smoke",
                    side_effect=lambda c, cfg, cid, m, **kw: (10, None),
                ):
            fake = FakeSleeper()
            Client.return_value = fake
            fake.on_position = lambda: seen.append(list(fake.ticks))
            code, out, _ = self.main(["--api-key", "k", "--character-id", "9", "--seconds", "10"])
        self.assertEqual(code, 0, out)
        self.assertEqual(seen, [[[{"verb": "Wait"}]]], "position read only after the Wait")

    def test_aim_at_puts_goto_first(self):
        cfg = config.load(REPO / "python" / "characters" / "olympuff_m7.toml")
        self.assertTrue(cfg.policy.pickup, "Recover needs pickup (A11)")
        self.smoke.aim_at(cfg, OVERWORLD, (5, 6))
        self.assertEqual(cfg.policy.goals, ["goto", "explore"])
        self.assertEqual((cfg.policy.goto, cfg.policy.goto_map), ((5, 6), OVERWORLD))


if __name__ == "__main__":
    unittest.main()
