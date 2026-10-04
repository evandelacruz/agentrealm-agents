"""What the agent decides, with no server. See PLAN.md."""

import random
import unittest

from agentrealm_agent.brain import Memory, choose_call, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.navigation import cost_path
from agentrealm_agent.navigation.rejection import NavMemory
from agentrealm_agent.world import Entity, WorldModel, terrain_cells
from agentrealm_agent.zone_discovery import apply_zone


def world(rows: list[str], at=(0, 0), perception=3) -> WorldModel:
    """A map from rows of glyphs: . dirt, # wall, D door, ~ lava. Everything given is known."""
    glyph = {".": "dirt", "#": "wall", "D": "framed_door", "~": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def scripted(**kw) -> Policy:
    return Policy(kind="scripted", **kw)


class PathTest(unittest.TestCase):
    def test_path_takes_diagonals_and_avoids_walls_and_occupants(self):
        # Movement is Chebyshev (internal/domain/movement.go): a diagonal is one step.
        w = world([
            "...",
            ".#.",
            "...",
        ])
        # Straight-line distance is 2; the wall in the middle makes it 3.
        self.assertEqual(len(cost_path(w, (2, 2))), 3)
        w.entities = [Entity("npc", 9, (1, 0))]
        p = cost_path(w, (2, 0))
        self.assertNotIn((1, 0), p)
        self.assertNotIn((1, 1), p)

    def test_unknown_ground_is_pathed_as_fog(self):
        w = world(["..", ".."])
        self.assertIsNotNone(cost_path(w, (5, 5)))


class ExploreTest(unittest.TestCase):
    def test_explore_heads_to_the_nearest_frontier(self):
        w = world([
            "....",
            "....",
        ])
        # Mark everything around the known strip as void except the east edge.
        for x in range(-1, 5):
            w.view.tiles.setdefault((x, -1), "")
            w.view.tiles.setdefault((x, 2), "")
        w.view.tiles[(-1, 0)] = w.view.tiles[(-1, 1)] = ""
        d = decide(w, Memory(), scripted(goals=["explore"]), random.Random(0))
        self.assertEqual(d.intent["verb"], "SetPosition")
        self.assertEqual(d.intent["x"], 1)

    def test_terrain_read_marks_missing_cells_as_known_void(self):
        w = WorldModel(character_id=1, pos=(1, 1), map_id=7)
        w.apply_terrain({"map_id": 7, "x0": 0, "y0": 0, "width": 3, "height": 3,
                         "legend": {"d": {"block_type": "dirt"}}, "rows": ["???", "?d?", "???"]})
        self.assertEqual(w.view.frontier(), set())

    def test_terrain_grid_decodes_cells_and_art(self):
        # docs/API.md Tiles (B102): rows are y0+j, symbols x0+i, art rides beside the grid.
        cells = terrain_cells({"x0": 4, "y0": 6, "legend": {"s": {"block_type": "statue"}, "g": {"block_type": "grass"}},
                               "rows": ["g?", "?s"], "art": [{"x": 5, "y": 7, "art": "statue_head", "facing": "left"}]})
        self.assertEqual(cells, {(4, 6): {"block_type": "grass"},
                                 (5, 7): {"block_type": "statue", "art": "statue_head", "facing": "left"}})


class ReflexTest(unittest.TestCase):
    def test_reflex_order(self):
        # One rule per case; the first matching rule in PLAN.md wins.
        cases = [
            ("lava underfoot beats a nearby hostile",
             ["~..", "...", "..."], (0, 0), [Entity("npc", 5, (2, 2))], scripted(),
             "SetPosition", lambda i: (i["x"], i["y"]) != (0, 0)),
            ("flee steps away from an npc",
             ["...", "...", "..."], (1, 1), [Entity("npc", 5, (2, 1))], scripted(),
             "SetPosition", lambda i: i["x"] == 0),
            ("fight swings at a character in range",
             ["...", "...", "..."], (1, 1), [Entity("character", 5, (2, 1), code="peer")],
             scripted(on_hostile="fight", hostile=["character"], hostile_range=1),
             "Use", lambda i: i["target"] == {"kind": "character", "character_id": 5}),
            ("take a supply in reach before walking",
             ["...", "...", "..."], (1, 1), [Entity("supply", 8, (1, 2))], scripted(goals=["goto"], goto=(2, 2)),
             "Take", lambda i: i["supply_id"] == 8),
            ("ignore leaves the plan in charge",
             ["...", "...", "..."], (0, 0), [Entity("npc", 5, (1, 0))],
             scripted(on_hostile="ignore", goals=["goto"], goto=(2, 2), pickup=False),
             "SetPosition", lambda i: (i["x"], i["y"]) == (1, 1)),
        ]
        for name, rows, at, ents, pol, verb, check in cases:
            with self.subTest(name):
                w = world(rows, at=at)
                w.entities = ents
                if name.startswith("fight swings"):
                    w.health, w.lives = 500, 10
                    w.threat.record(("character", "peer"), 1)
                    from agentrealm_agent.directives import PARAM_DEFAULTS

                    d = decide(
                        w,
                        Memory(),
                        pol,
                        random.Random(0),
                        params={**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1},
                    )
                else:
                    d = decide(w, Memory(), pol, random.Random(0))
                self.assertIsNotNone(d.intent, d.reason)
                self.assertEqual(d.intent["verb"], verb, d.reason)
                self.assertTrue(check(d.intent), d.intent)

    def test_no_goal_sends_nothing(self):
        # Invariant 4: no standing orders. Nothing to do means no intent.
        w = world(["..."])
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(d.intent)

    def test_replanning_keeps_off_a_rejected_tile(self):
        # Reflex 1 (PLAN.md): after a rejected step, the replan does not
        # walk straight back into the same tile on the next decision.
        cases = [
            ("goto", scripted(goals=["goto"], goto=(2, 0), pickup=False)),
            ("explore", scripted(goals=["explore"], pickup=False)),
            ("wander", scripted(goals=["wander"], pickup=False)),
        ]
        for name, pol in cases:
            with self.subTest(name):
                w = world([".....", "....."])
                m = Memory(nav=NavMemory(wait_tile=(w.map_id, (1, 0))))
                d = decide(w, m, pol, random.Random(0))
                self.assertIsNotNone(d.intent, d.reason)
                self.assertNotEqual((d.intent["x"], d.intent["y"]), (1, 0), d.reason)
                self.assertIsNone(m.nav.wait_tile, "the block lasts one decision")

    def test_plan_keeps_off_blocks_to_avoid(self):
        # avoid_blocks are walkable, so without this the plan walks into lava
        # and reflex 2 steps back out.
        w = world(["...", ".~.", "..."])
        d = decide(w, Memory(), scripted(goals=["goto"], goto=(2, 2), pickup=False), random.Random(0))
        self.assertNotEqual((d.intent["x"], d.intent["y"]), (1, 1), d.reason)

    def test_surrounded_by_lava_the_plan_crosses_as_little_as_it_can(self):
        # Straight east is 3 steps over 2 lava tiles; via the top row it is
        # 4 steps over 1.
        w = world([".....", "~~~~.", "~~~~.", "~~~~."], at=(1, 2))
        m = Memory()
        d = decide(w, m, scripted(goals=["goto"], goto=(4, 2), pickup=False), random.Random(0))
        self.assertEqual((d.intent["x"], d.intent["y"]), (2, 1), d.reason)
        self.assertEqual(m.path, [(2, 1), (3, 0), (4, 1), (4, 2)])

    def test_doors_goal_steps_onto_the_door(self):
        w = world(["..D"])
        d = decide(w, Memory(), scripted(goals=["doors"]), random.Random(0))
        self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))


class DeathChestTest(unittest.TestCase):
    """B103: Died names the dropped chest; the agent goes back and empties it."""

    def test_recover_death_chest(self):
        w = world(["....."], at=(4, 0))
        w.apply_events([{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}])
        self.assertEqual(w.death_chest, (7, (0, 0), 80))
        w.map_id, w.pos = 7, (4, 0)  # respawned along the strip
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})

        m = Memory()
        d = decide(w, m, scripted(goals=["hold"]), random.Random(0))
        self.assertEqual((d.intent["verb"], d.intent["x"]), ("SetPosition", 3))

        # Next to it, the contents are not known until a snapshot lists them.
        w.pos = (1, 0)
        self.assertIsNone(decide(w, Memory(), scripted(goals=["hold"]), random.Random(0)).intent)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [
            {"id": 80, "x": 0, "y": 0, "contents": [{"id": 1321, "supply_subtype_code": "bronze_sword"}]},
        ]}}})
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertEqual(d.intent, {"verb": "WithdrawFromChest", "chest_id": 80})

        # Emptied, a dropped chest leaves the world (B116): gone from the
        # snapshot while its block is within reach, it is forgotten rather
        # than waited on.
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": []}}})
        self.assertIsNone(w.death_chest)

    def test_death_chest_out_of_reach_is_kept(self):
        """Out of reach, a chest missing from the snapshot may only be out of sight."""
        w = world(["....."], at=(4, 0))
        w.apply_events([{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}])
        w.map_id, w.pos = 7, (4, 0)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": []}}})
        self.assertEqual(w.death_chest, (7, (0, 0), 80))


class TerrainStaleTest(unittest.TestCase):
    def test_triggers(self):
        w = world(["...."], at=(0, 0), perception=5)
        self.assertFalse(w.terrain_stale(), "fresh after world() seeds terrain at pos")
        w.terrain_center = (0, 0)
        self.assertFalse(w.terrain_stale(), "still at the read center")
        w.pos = (2, 0)
        self.assertFalse(w.terrain_stale(), "half the window is perception // 2")
        w.pos = (3, 0)
        self.assertTrue(w.terrain_stale(), "past half the perception window")
        w.pos = (0, 0)
        w.map_id = 8
        self.assertTrue(w.terrain_stale(), "map change")
        w.map_id = 7
        w.terrain_map = None
        self.assertTrue(w.terrain_stale(), "never read on this map")


class SchedulerTest(unittest.TestCase):
    def test_call_choice(self):
        pol = scripted(entity_refresh=5, hostile=["npc"])
        cases = [
            ("self first", dict(need_self=True), {}, "self"),
            ("then position", dict(need_self=False, need_position=True), {}, "position"),
            ("terrain after moving half the perception range", dict(need_self=False, need_position=False),
             dict(terrain_center=(0, 0), pos=(2, 0), perception=3, entities_tick=10, tick=10), "terrain"),
            ("terrain on map change", dict(need_self=False, need_position=False),
             dict(terrain_center=(0, 0), terrain_map=7, map_id=8, pos=(0, 0), entities_tick=10, tick=10), "terrain"),
            ("entities when stale", dict(need_self=False, need_position=False),
             dict(entities_tick=0, tick=5), "entities"),
            ("entities on alarm", dict(need_self=False, need_position=False, alarm=True),
             dict(entities_tick=10, tick=10), "entities"),
            ("terrain before entities when both are stale", dict(need_self=False, need_position=False),
             dict(terrain_center=(0, 0), pos=(2, 0), perception=3, entities_tick=0, tick=5), "terrain"),
            ("calm skips inside the gap", dict(need_self=False, need_position=False, last_poll_tick=10, calm_poll_interval=7),
             dict(entities_tick=10, tick=12), "skip"),
            ("calm spare window probes safe tiles when town is known",
             dict(need_self=False, need_position=False, last_poll_tick=10, calm_poll_interval=7),
             dict(entities_tick=10, tick=12, respawn_anchors=[(7, (0, 0))]), "zone"),
            ("a spare calm window reads stale terrain",
             dict(need_self=False, need_position=False, last_poll_tick=10, calm_poll_interval=7),
             dict(terrain_center=(0, 0), pos=(3, 0), perception=5, entities_tick=10, tick=12), "terrain"),
            ("a hostile within 3 blocks polls inside the gap",
             dict(need_self=False, need_position=False, last_poll_tick=10, calm_poll_interval=7),
             dict(entities=[Entity("npc", 9, (3, 0))], entities_tick=10, tick=11), "tick"),
            ("otherwise tick", dict(need_self=False, need_position=False),
             dict(entities_tick=10, tick=12), "tick"),
        ]
        for name, mem, wkw, want in cases:
            with self.subTest(name):
                w = world(["...."], at=(0, 0), perception=3)
                for k, v in wkw.items():
                    setattr(w, k, v)
                self.assertEqual(choose_call(w, Memory(**mem), pol), want)


if __name__ == "__main__":
    unittest.main()
