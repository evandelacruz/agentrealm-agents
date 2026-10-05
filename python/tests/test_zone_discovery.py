"""Safe-tile discovery (A7)."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.brain import Memory, choose_call
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.world import Entity, WorldModel, chebyshev
from agentrealm_agent.zone_discovery import (
    RESPAWN_PROBE_RADIUS,
    apply_town,
    apply_zone,
    next_zone_probe,
    safe_tiles,
)


def record_respawn_anchor(w: WorldModel, map_id: int, pos) -> None:
    w.record_respawn_anchor(map_id, pos)


def calm_mem(**kw) -> Memory:
    defaults = dict(need_self=False, need_position=False, last_poll_tick=10, calm_poll_interval=7)
    defaults.update(kw)
    return Memory(**defaults)


def filled_world(at=(5, 5), perception=5) -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for dy in range(-perception, perception + 1):
        for dx in range(-perception, perception + 1):
            w.view.tiles[(at[0] + dx, at[1] + dy)] = "grass"
    w.terrain_center, w.terrain_map = at, 7
    w.entities_tick = 0
    return w


class ZoneDiscoveryTest(unittest.TestCase):
    def test_apply_zone_records_safe_tiles(self):
        w = filled_world()
        apply_zone(w, 7, 0, 0, {"safe": True, "brightness": 1})
        apply_zone(w, 7, 1, 0, {"safe": False, "brightness": 1})
        self.assertEqual(safe_tiles(w, 7), {(0, 0)})
        self.assertEqual(safe_tiles(w, 8), set())

    def test_respawn_anchor_seeds_probes_before_path(self):
        w = filled_world(at=(10, 10))
        record_respawn_anchor(w, 7, (8, 8))
        apply_zone(w, 7, 8, 8, {"safe": True, "brightness": 1})
        # (11, 10) is a step away but on the path; the anchor's ring comes first.
        nxt = next_zone_probe(w, Memory(path=[(11, 10)]))
        self.assertEqual(nxt, (7, (7, 7)), "the revealed cell nearest the anchor")

    def test_unrevealed_cells_are_never_probed(self):
        w = filled_world(at=(20, 20))
        record_respawn_anchor(w, 7, (0, 0))  # its whole ring is unrevealed
        self.assertIsNone(next_zone_probe(w, Memory()))

    def test_failed_cell_is_not_probed_again(self):
        w = filled_world(at=(5, 5))
        record_respawn_anchor(w, 7, (5, 5))
        first = next_zone_probe(w, Memory())
        w.zone_failed.add(first)
        self.assertNotEqual(next_zone_probe(w, Memory()), first)

    def test_path_cells_are_probed_after_respawn_area(self):
        w = filled_world(at=(0, 0), perception=12)
        anchor = (0, 0)
        record_respawn_anchor(w, 7, anchor)
        for pos in list(w.view.tiles):
            if chebyshev(pos, anchor) <= RESPAWN_PROBE_RADIUS:
                apply_zone(w, 7, pos[0], pos[1], {"safe": False, "brightness": 1})
        nxt = next_zone_probe(w, Memory(path=[(0, 9), (0, 10)]))
        self.assertEqual(nxt, (7, (0, 9)))

    def test_town_from_world_read_becomes_anchor(self):
        w = WorldModel(character_id=1)
        apply_town(w, {"map_id": 3, "x": 10, "y": 20})
        self.assertEqual(w.respawn_anchors, [(3, (10, 20))])

    def test_respawned_event_becomes_anchor(self):
        w = WorldModel(character_id=1)
        w.apply_events([{"tick": 5, "events": [
            {"kind": "Respawned", "map_id": 3, "x": 4, "y": 6},
            {"kind": "Respawned", "map_id": 3, "x": 4, "y": 6},
            {"kind": "Respawned"},  # no location: ignored, not a KeyError
        ]}])
        self.assertEqual(w.respawn_anchors, [(3, (4, 6))])

    def test_calm_skip_becomes_zone_when_probes_pending(self):
        w = filled_world(at=(5, 5))
        record_respawn_anchor(w, 7, (5, 5))
        m = Memory(
            need_self=False,
            need_position=False,
            last_poll_tick=10,
            calm_poll_interval=7,
        )
        w.tick = 12
        w.entities_tick = 12
        self.assertEqual(choose_call(w, m, Policy()), "zone")
        self.assertEqual(m.zone_probe, (7, (5, 5)), "the pick rides to the runner, computed once")

    def test_urgent_windows_never_read_zones(self):
        w = filled_world(at=(5, 5))
        record_respawn_anchor(w, 7, (5, 5))
        w.tick = w.entities_tick = 12
        hostile = Policy(hostile=["npc"])
        w.entities = [Entity("npc", 9, (6, 5))]
        self.assertEqual(choose_call(w, calm_mem(), hostile), "tick")
        w.entities = []
        self.assertEqual(choose_call(w, calm_mem(hurt_last_poll=True), Policy()), "tick")
        m = calm_mem()
        self.assertEqual(choose_call(w, m, Policy()), "zone")

    def test_idle_reads_no_entities_or_zones(self):
        w = filled_world(at=(5, 5))
        record_respawn_anchor(w, 7, (5, 5))
        w.tick, w.entities_tick = 12, -1000  # entities long stale
        idle = Policy(kind="idle")
        self.assertEqual(choose_call(w, calm_mem(), idle), "skip")
        self.assertEqual(choose_call(w, calm_mem(last_poll_tick=5), idle), "tick")


class FakeServer:
    """Paced windows, nothing in sight; zone reads refused for some cells."""

    def __init__(self, windows: int, stop: threading.Event, refuse=()):
        self.tick_now = 100
        self.windows = windows
        self.stop = stop
        self.refuse = set(refuse)
        self.calls: list[tuple[int, str, tuple | None]] = []

    def wait(self, not_before: float = 0.0) -> None:
        self.windows -= 1
        if self.windows < 0:
            self.stop.set()
        self.tick_now += 1

    def world(self, cid):
        return {"tick_rate_hz": 10, "town": {"map_id": 7, "x": 0, "y": 0}}

    def tick(self, cid, intents, *, snapshot_version=None):
        self.calls.append((self.tick_now, "tick", None))
        return {"tick": self.tick_now, "window_remaining_ms": 0, "queue_id": "q"}

    def entities(self, cid, map_id, *rect):
        self.calls.append((self.tick_now, "entities", None))
        return {"tick": self.tick_now}

    def zone(self, cid, map_id, x, y):
        self.calls.append((self.tick_now, "zone", (map_id, (x, y))))
        if (map_id, (x, y)) in self.refuse:
            raise ApiError(404, "not_revealed")
        return {"tick": self.tick_now, "safe": (x, y) == (0, 0), "brightness": 1}


class RunnerZoneTest(unittest.TestCase):
    """Runs Runner.run: calm spare windows probe zones, one call per window."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def run_windows(self, windows: int, refuse=()):
        stop = threading.Event()
        server = FakeServer(windows, stop, refuse)
        pol = Policy(goals=["hold"], entity_refresh=1000)
        cfg = CharacterConfig("T", "sandbox", pol, Path("t.toml"))
        r = runner.Runner(cfg, server, 1, stop, out=lambda _: None)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=25, tick=100)
        for y in range(-1, 2):
            for x in range(-1, 2):
                w.view.tiles[(x, y)] = "dirt"
        w.terrain_center, w.terrain_map, w.entities_tick = (0, 0), 7, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: server.wait(nb)):
            r.run()
        return server, r.world

    def test_spare_windows_probe_the_town_ring(self):
        s, w = self.run_windows(30)
        probed = [c[2] for c in s.calls if c[1] == "zone"]
        self.assertEqual(len(probed), 9, "each revealed cell around town once")
        self.assertEqual(len(set(probed)), 9)
        self.assertEqual(safe_tiles(w, 7), {(0, 0)})
        self.assertTrue(all(b[0] > a[0] for a, b in zip(s.calls, s.calls[1:])), "one call per window")
        polls = [t for t, call, _ in s.calls if call == "tick"]
        gaps = [b - a for a, b in zip(polls, polls[1:])]
        self.assertTrue(all(4 <= g <= 10 for g in gaps), gaps)

    def test_refused_cell_is_not_reprobed(self):
        s, w = self.run_windows(30, refuse=[(7, (0, 0))])
        probed = [c[2] for c in s.calls if c[1] == "zone"]
        self.assertEqual(probed.count((7, (0, 0))), 1)
        self.assertEqual(len(probed), 9)
        self.assertIn((7, (0, 0)), w.zone_failed)
        self.assertEqual(safe_tiles(w, 7), set())


class ClientZoneTest(unittest.TestCase):
    def test_zone_query_on_wire(self):
        import json
        from unittest.mock import MagicMock, patch

        from agentrealm_agent.client import Client

        client = Client("https://example.test", "key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"safe": True, "brightness": 1}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as urlopen:
            client.zone(4, 7, 1, 2)

        req = urlopen.call_args[0][0]
        self.assertIn("characters/4/zone", req.full_url)
        self.assertIn("map_id=7", req.full_url)
        self.assertIn("x=1", req.full_url)
        self.assertIn("y=2", req.full_url)


if __name__ == "__main__":
    unittest.main()
