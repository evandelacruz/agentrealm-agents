"""Safe-tile discovery (A7)."""

import unittest

from agentrealm_agent.brain import Memory, choose_call
from agentrealm_agent.config import Policy
from agentrealm_agent.zone_discovery import (
    RESPAWN_PROBE_RADIUS,
    apply_town,
    apply_zone,
    nearest_known_safe,
    next_zone_probe,
    record_respawn_anchor,
)
from agentrealm_agent.world import chebyshev
from agentrealm_agent.world import WorldModel


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
        self.assertIn((0, 0), w.safe_tiles[7])
        self.assertNotIn((1, 0), w.safe_tiles.get(7, ()))

    def test_respawn_anchor_seeds_probes_before_path(self):
        w = filled_world(at=(10, 10))
        record_respawn_anchor(w, 7, (0, 0))
        apply_zone(w, 7, 0, 0, {"safe": True, "brightness": 1})
        nxt = next_zone_probe(w, Memory(path=[(12, 10)]))
        self.assertEqual(nxt, (7, (5, 5)), "respawn ring uses revealed cells nearest the anchor")

    def test_path_cells_are_probed_after_respawn_area(self):
        w = filled_world(at=(0, 0), perception=12)
        anchor = (0, 0)
        record_respawn_anchor(w, 7, anchor)
        for pos in list(w.view.tiles):
            if chebyshev(pos, anchor) <= RESPAWN_PROBE_RADIUS:
                apply_zone(w, 7, pos[0], pos[1], {"safe": False, "brightness": 1})
        nxt = next_zone_probe(w, Memory(path=[(0, 9), (0, 10)]))
        self.assertEqual(nxt, (7, (0, 9)))

    def test_nearest_known_safe(self):
        w = filled_world(at=(0, 0))
        apply_zone(w, 7, 3, 0, {"safe": True, "brightness": 1})
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        found = nearest_known_safe(w)
        self.assertIsNotNone(found)
        target, path = found
        self.assertEqual(target, (1, 0))
        self.assertEqual(path[-1], target)

    def test_town_from_world_read_becomes_anchor(self):
        w = WorldModel(character_id=1)
        apply_town(w, {"map_id": 3, "x": 10, "y": 20})
        self.assertEqual(w.respawn_anchors, [(3, (10, 20))])

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
