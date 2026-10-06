"""A65: park on safe ground before the run exits, then clear the queue."""

import io
import signal
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from agentrealm_agent import __main__ as cli
from agentrealm_agent import acceptance_smoke, config, runner
from agentrealm_agent.acceptance import ParkSplit
from agentrealm_agent.brain import Memory
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.executor.movement import step_landing
from agentrealm_agent.park import (
    PARK_ABORTED,
    PARK_DIED,
    PARK_DOWNED,
    PARK_NOWHERE,
    PARK_TIMED_OUT,
    PARKED_SAFE,
    ParkReport,
    install_stop_signals,
)
from agentrealm_agent.states.dispatch import PARK_STATES
from agentrealm_agent.world import WorldModel
from agentrealm_agent.zone_discovery import ZoneFact

MAP = 7
SAFE = (6, 0)


class WalkServer:
    """A server whose queues run at once: every Step lands, then the queue
    finishes. ``moves=False`` takes queues and never moves the character."""

    def __init__(self, pos=(0, 0), *, moves=True, died_at_poll: int | None = None, drop_died=False):
        self.pos, self.moves, self.died_at_poll, self.drop_died = pos, moves, died_at_poll, drop_died
        self.alive = True
        self.tick_now = 100
        self.sent: list[list[dict] | None] = []
        self.polls = 0

    def wait(self, not_before: float = 0.0) -> None:
        self.tick_now += 1

    def world(self, cid):
        return {"tick_rate_hz": 10}

    def self_(self, cid):
        return {"alive": self.alive, "placed": self.alive, "perception_range": 25}

    def position(self, cid):
        return {"map_id": MAP, "x": self.pos[0], "y": self.pos[1]}

    def entities(self, cid, map_id, *rect):
        return {"tick": self.tick_now}

    def zone(self, cid, map_id, x, y):
        return {"tick": self.tick_now, "safe": False, "brightness": 1}

    def tick(self, cid, intents, *, snapshot_version=None):
        self.sent.append(intents)
        self.polls += 1
        r = {"tick": self.tick_now, "window_remaining_ms": 0}
        if self.died_at_poll == self.polls:
            self.alive = False
            if self.drop_died:
                r["events_dropped"] = 1
            else:
                r["events_by_tick"] = [{"tick": self.tick_now, "events": [{"kind": "Died"}]}]
            return r
        if not intents:
            return r
        qid = f"q{len(self.sent)}"
        r["queue_id"] = qid
        if not self.moves:
            r["queue"] = {"queue_id": qid, "next_index": 0}
            return r
        results = []
        for i, intent in enumerate(intents):
            if intent["verb"] == "Step":
                self.pos = step_landing(self.pos, intent["direction"])
                results.append({"queue_id": qid, "index": i, "tick": self.tick_now, "outcome": "applied"})
        r["intent_results"] = results
        r["finished_queue"] = {"queue_id": qid}
        return r


class Hooks(ParkSplit):
    """Counts what the scenario hooks hear; parking must leave them silent."""

    def __init__(self, stop_after_windows: int):
        super().__init__()
        self.stop_after_windows = stop_after_windows
        self.windows = self.ticks = self.steps = 0
        self.at_park_start: tuple[int, int, int] | None = None

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        self.windows += 1
        if self.windows >= self.stop_after_windows:
            self.stop.set()  # the run's time limit

    def before_tick(self, *args, **kwargs) -> None:
        self.ticks += 1

    def on_step_applied(self) -> None:
        self.steps += 1

    def on_park_start(self) -> None:
        self.at_park_start = (self.windows, self.ticks, self.steps)
        super().on_park_start()


class ParkTest(unittest.TestCase):
    """Runs Runner.park itself: only the fake server's clock moves."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def make(self, server, *, at=(0, 0), safe=(SAFE,), park_seconds=60.0, acceptance=None, stop=None):
        cfg = CharacterConfig("T", "sandbox", Policy(goals=[], entity_refresh=1000), Path("t.toml"))
        r = runner.Runner(
            cfg, server, 1, stop or threading.Event(), out=lambda _: None, acceptance=acceptance, park_seconds=park_seconds
        )
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=25, tick=100)
        for y in range(-3, 4):
            for x in range(-3, 12):
                w.view.tiles[(x, y)] = "dirt"
        w.zones[MAP] = {p: ZoneFact(safe=True) for p in safe}
        w.terrain_center, w.terrain_map, w.entities_tick = at, MAP, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.pacer.wait_next_window = server.wait
        return r

    def test_already_safe_exits_at_once_and_clears_the_queue(self):
        s = WalkServer(pos=SAFE)
        r = self.make(s, at=SAFE)
        report = r.park()
        self.assertEqual(report.outcome, PARKED_SAFE)
        self.assertEqual(s.sent, [[]], "one empty tick, nothing walked")
        self.assertTrue(report.queue_cleared)
        self.assertFalse(r.mem.parking)

    def test_already_safe_still_clears_a_scenario_queue(self):
        # Stopped mid-walk on safe ground: the walk queue must not run on.
        s = WalkServer(pos=SAFE)
        r = self.make(s, at=SAFE)
        r.mem.pending_intents, r.mem.pending_queue = [{"verb": "Step", "direction": "east"}], "old"
        r.mem.held_queue = {"queue_id": "old", "next_index": 0}
        report = r.park()
        self.assertEqual(report.outcome, PARKED_SAFE)
        self.assertEqual(s.sent, [[]])
        self.assertIsNone(r.mem.held_queue)
        self.assertIsNone(r.mem.pending_intents)

    def test_unsafe_walks_to_the_safe_tile_then_exits(self):
        s = WalkServer()
        r = self.make(s)
        report = r.park()
        self.assertEqual(report.outcome, PARKED_SAFE)
        self.assertEqual(r.world.pos, SAFE)
        self.assertEqual(s.pos, SAFE)
        steps = [i for q in s.sent if q for i in q if i["verb"] == "Step"]
        self.assertEqual(len(steps), SAFE[0], "Retreat's path: straight east")
        self.assertEqual(s.sent[-1], [], "the queue is cleared last")
        self.assertEqual(report.pos, SAFE)

    def test_timeout_exits_anyway_and_clears_the_queue(self):
        s = WalkServer(moves=False)
        r = self.make(s, park_seconds=5.0)
        now = iter(range(1000))
        r.clock = lambda: float(next(now))
        report = r.park()
        self.assertEqual(report.outcome, PARK_TIMED_OUT)
        self.assertNotEqual(r.world.pos, SAFE)
        self.assertTrue(any(q and q[0]["verb"] in ("Step", "Wait") for q in s.sent[:-1]), "it tried to walk")
        self.assertEqual(s.sent[-1], [])
        self.assertTrue(report.queue_cleared)
        self.assertIsNone(r.mem.held_queue)

    def test_abort_cuts_the_park_short(self):
        s = WalkServer()
        r = self.make(s)
        r.abort.set()
        report = r.park()
        self.assertEqual(report.outcome, PARK_ABORTED)
        self.assertEqual(s.sent, [[]], "the queue is still cleared")

    def test_nowhere_safe_known_exits_at_once(self):
        s = WalkServer()
        r = self.make(s, safe=())
        report = r.park()
        self.assertEqual(report.outcome, PARK_NOWHERE)
        self.assertEqual(s.sent, [[]])

    def test_a_death_while_parking_ends_it_and_is_reported(self):
        s = WalkServer(died_at_poll=1)
        r = self.make(s)
        report = r.park()
        self.assertEqual(report.outcome, PARK_DIED)
        self.assertEqual(s.sent[-1], [])

    def test_a_death_whose_event_was_dropped_is_still_a_park_death(self):
        # Only the next self read shows it: the Died event never arrived.
        s = WalkServer(died_at_poll=1, drop_died=True)
        r = self.make(s)
        real_tick = r.tick

        def tick():
            out = real_tick()
            if not s.alive:
                r.mem.need_self = True  # the periodic self refresh, brought forward
            return out

        r.tick = tick
        report = r.park()
        self.assertEqual(report.outcome, PARK_DIED)

    def test_downed_before_the_park_is_not_a_park_death(self):
        s = WalkServer()
        r = self.make(s)
        r.world.alive = False
        report = r.park()
        self.assertEqual(report.outcome, PARK_DOWNED)
        self.assertEqual(s.sent, [[]])

    def test_clear_queue_error_is_reported_not_raised(self):
        s = WalkServer(pos=SAFE)
        r = self.make(s, at=SAFE)

        def refuse(cid, intents, *, snapshot_version=None):
            raise ApiError(409, "character_not_live", "dead")

        s.tick = refuse
        report = r.park()
        self.assertEqual(report.outcome, PARKED_SAFE)
        self.assertFalse(report.queue_cleared)
        self.assertIn("queue NOT cleared", report.line())

    def test_run_parks_after_the_stop_and_hooks_stay_silent(self):
        stop = threading.Event()
        s = WalkServer()
        hooks = Hooks(stop_after_windows=2)
        hooks.stop = stop
        r = self.make(s, acceptance=hooks, stop=stop)
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: s.wait(nb)):
            r.run()
        self.assertEqual(r.park_report.outcome, PARKED_SAFE)
        self.assertEqual(s.pos, SAFE)
        self.assertIs(hooks.park, r.park_report)
        self.assertEqual(hooks.at_park_start, (hooks.windows, hooks.ticks, hooks.steps), "the park is not the scenario")
        self.assertEqual(s.sent[-1], [])

    def test_run_with_parking_off_sends_nothing_after_the_stop(self):
        stop = threading.Event()
        s = WalkServer()
        r = self.make(s, park_seconds=0.0, stop=stop)
        stop.set()
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: s.wait(nb)):
            r.run()
        self.assertIsNone(r.park_report)
        self.assertEqual(s.sent, [])


class ParkStatesTest(unittest.TestCase):
    def test_only_survival_reflexes_and_park_run_while_parking(self):
        names = [s.name for s in PARK_STATES]
        self.assertEqual(names, ["Sync", "Downed", "Escape", "Retreat", "Heal", "Fight", "Flee", "Park"])


class ParkSplitTest(unittest.TestCase):
    def test_api_errors_while_parking_are_kept_apart(self):
        hooks = ParkSplit()
        hooks.api_errors.append("tick 503 overloaded")
        hooks.on_park_start()
        hooks.api_errors.append("tick 409 character_not_live")
        report = ParkReport(PARK_TIMED_OUT, 60.0, MAP, (1, 0), True)
        hooks.on_park_end(report)
        self.assertEqual(hooks.api_errors, ["tick 503 overloaded"])
        self.assertEqual(hooks.park_api_errors, ["tick 409 character_not_live"])
        line = hooks.park_summary_line()
        self.assertIn("park timed out at 7:1,0", line)
        self.assertIn("1 API error(s) while parking", line)

    def test_a_death_while_parking_does_not_fail_the_alive_check(self):
        client = mock.Mock()
        client.self_.return_value = {"alive": False}
        hooks = ParkSplit()
        hooks.park = ParkReport(PARK_DIED, 3.0, MAP, (1, 0), False)
        self.assertEqual(acceptance_smoke.alive_at_end_failures(client, 1, hooks), [])
        hooks.park = ParkReport(PARKED_SAFE, 3.0, MAP, SAFE, True)
        self.assertEqual(acceptance_smoke.alive_at_end_failures(client, 1, hooks), ["character not alive at end"])


class StopSignalsTest(unittest.TestCase):
    def test_first_signal_stops_second_aborts(self):
        stop, abort, lines = threading.Event(), threading.Event(), []
        before = signal.getsignal(signal.SIGTERM)
        restore = install_stop_signals(stop, abort, lines.append, 60.0)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
            self.assertFalse(abort.is_set())
            self.assertIn("parking on safe ground first", lines[-1])
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(abort.is_set())
        finally:
            restore()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


    def test_second_signal_exits_even_with_a_wedged_driver(self):
        # The runner never returns (a hung HTTP call, say): the second signal
        # must still end ``run`` after the bounded join, not wait on it.
        release = threading.Event()
        self.addCleanup(release.set)

        class WedgedRunner:
            def __init__(self, *args, **kwargs):
                pass

            def run(self):
                release.wait(30)

        def signal_twice():
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)

        client = mock.Mock()
        cfg = SimpleNamespace(profile="T", world="sandbox")
        with mock.patch.object(cli, "Runner", WedgedRunner), \
                mock.patch.object(cli, "SHUTDOWN_JOIN_SECONDS", 0.1), \
                mock.patch.object(cli, "load_knowledge"), \
                mock.patch.object(cli, "save_knowledge"), \
                redirect_stdout(io.StringIO()) as out:
            timer = threading.Timer(0.2, signal_twice)
            timer.start()
            started = time.monotonic()
            code = cli.run(client, cfg, 1, park_seconds=60.0)
            elapsed = time.monotonic() - started
        timer.join()
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 3.0)
        self.assertIn("SIGTERM again", out.getvalue())


if __name__ == "__main__":
    unittest.main()
