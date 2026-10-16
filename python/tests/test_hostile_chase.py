"""A162: hostiles are chasers and patrollers, not guards (Manual §11 Hostiles, Combat).

- A chaser steps to the neighbouring block that brings it closest to the
  character and stands still where none does; a still hostile is blocked,
  not guarding (``hostile_ground.chase``, ``Sighting.still``).
- A pass through a hostile's ground prices the chaser's approach: one in
  the open keeps up and comes within range, one the ground stops does not
  (``pathing.covers_pass``).
- A hostile never heals while it lives: the damage it has taken is kept
  until it dies (``WorldModel.npc_damage``, ``survival.health_left``).
- Safety is judged on the exact block: hazard and danger cost nothing on a
  known safe block (``navigation.planner``, ``pathing.plan_sets``,
  ``hostile_ground.walk_ground``).
"""

from __future__ import annotations

import unittest

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.engagement import decide
from agentrealm_adk.hostile_ground import chase, chaser_step, walk_ground
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation.planner import COSTLY_STEP, KNOWN_WALKABLE, CostGridParams, _Grid
from agentrealm_adk.pathing import covers_pass, plan_sets
from agentrealm_adk.survival import UNKILLED_HOSTILE_HEALTH, health_left, ticks_to_kill_them, win_ratio
from agentrealm_adk.world import STILL_TICKS, Entity, WorldModel
from agentrealm_adk.zone_discovery import apply_zone

MAP = 7
CODE = "fake_gnawer"
GLYPHS = {".": "dirt", "#": "wall", "~": "water", "l": "lava"}
POLICY = Policy(kind="scripted", hostile=["npc"], on_hostile="fight", avoid_blocks=["lava"])


def world(rows: list[str], at: tuple[int, int]) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=12)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = GLYPHS[ch]
    w.terrain_center, w.terrain_map = at, MAP
    w.alive, w.health, w.max_health, w.lives = True, 10, 10, 9
    w.hostile_types.add(("npc", CODE))
    return w


def gnawer(at: tuple[int, int], npc_id: int = 9) -> Entity:
    return Entity("npc", npc_id, at, CODE)


def safe(w: WorldModel, *cells: tuple[int, int]) -> None:
    for x, y in cells:
        apply_zone(w, MAP, x, y, {"safe": True, "brightness": 1})


class ChaseTest(unittest.TestCase):
    def test_in_the_open_it_steps_to_the_closest_neighbour(self):
        w = world(["." * 9] * 5, at=(8, 2))
        self.assertEqual(chaser_step((0, 2), (8, 2), w.chaser_ground()), (1, 2))
        self.assertEqual(chaser_step((0, 0), (8, 2), w.chaser_ground()), (1, 1), "straight-line breaks a Chebyshev tie")
        self.assertEqual(chase(w, (0, 2), [(8, 2), (7, 2), (6, 2)]), [(1, 2), (2, 2), (3, 2)])

    def test_it_never_steps_onto_the_character(self):
        w = world(["..."], at=(2, 0))
        self.assertEqual(chaser_step((1, 0), (2, 0), w.chaser_ground()), (1, 0))

    def test_a_wall_or_water_between_stops_it_where_no_step_closes_in(self):
        """It finds no path: blocked, it stands still, however near the way round."""
        w = world(["#####", "#.#.#", "#.#.#", "#...#", "#####"], at=(3, 1))
        self.assertEqual(chaser_step((1, 1), (3, 1), w.chaser_ground()), (1, 1))
        w = world(["#####", "#.~.#", "#####"], at=(3, 1))
        self.assertEqual(chaser_step((1, 1), (3, 1), w.chaser_ground()), (1, 1), "a hostile fords nothing")

    def test_unseen_ground_may_carry_it(self):
        w = world(["."], at=(0, 0))
        self.assertEqual(chaser_step((3, 0), (0, 0), w.chaser_ground()), (2, 0))


class StillIsBlockedTest(unittest.TestCase):
    def test_one_that_stands_still_is_blocked_and_one_that_moves_is_not(self):
        w = world(["." * 9] * 5, at=(1, 2))
        e = gnawer((6, 2))
        for t in range(STILL_TICKS + 1):
            w.tick = t
            w._set_entities([e], t)
        self.assertTrue(w.sightings[("npc", 9)].still)
        moved = gnawer((5, 2))
        w.tick += 1
        w._set_entities([moved], w.tick)
        self.assertFalse(w.sightings[("npc", 9)].still, "it stepped: not blocked")


class ChaserApproachIsPricedTest(unittest.TestCase):
    """A corridor along y 1 past a hostile three rows below: walled in, it
    cannot come nearer and the pass is covered; with a way up to the
    corridor, it comes within range of the walk and the pass is not."""

    WALLED = ["#" * 23, "#" + "." * 21 + "#", "#" * 23, "#" * 23, "#" * 12 + "." + "#" * 10, "#" * 23]
    OPEN = ["#" * 23, "#" + "." * 21 + "#", "#" * 12 + "." + "#" * 10, "#" * 12 + "." + "#" * 10, "#" * 12 + "." + "#" * 10, "#" * 23]
    PATH = [(x, 1) for x in range(2, 22)]

    def covers(self, rows: list[str]) -> bool:
        w = world(rows, at=(1, 1))
        w.max_health = w.health = 100
        w.threat.record(("npc", CODE), 2)
        w.entities = [gnawer((12, 4))]
        ground = walk_ground(w, POLICY)
        reach = {p for g in ground.values() for p in g.cells}
        return covers_pass(w, POLICY, dict(PARAM_DEFAULTS), self.PATH, reach, 10, ground)

    def test_a_blocked_chaser_is_passed(self):
        self.assertTrue(self.covers(self.WALLED))

    def test_a_chaser_with_a_way_to_the_walk_is_not(self):
        self.assertFalse(self.covers(self.OPEN))


class KeptDamageTest(unittest.TestCase):
    def damaged(self, w: WorldModel, npc_id: int, amount: int) -> None:
        w.learn_threat([{"kind": "NPCDamaged", "npc_id": npc_id, "amount": amount}], w.entities)

    def test_every_hit_it_takes_is_kept_until_it_dies(self):
        w = world(["..."], at=(0, 0))
        e = gnawer((2, 0))
        w.entities = [e]
        self.assertEqual(health_left(e, w.npc_damage), UNKILLED_HOSTILE_HEALTH, "never hit: the assumed health")
        self.damaged(w, 9, 3)
        self.damaged(w, 9, 2)  # anyone's swing: it never heals
        self.assertEqual(w.npc_damage, {9: 5})
        self.assertEqual(health_left(e, w.npc_damage), UNKILLED_HOSTILE_HEALTH - 5)
        w.learn_threat([{"kind": "NPCDied", "npc_id": 9, "npc_type": CODE}], [])
        self.assertEqual(w.npc_damage, {}, "it returns at full health")

    def test_state_shows_the_health_it_has_left(self):
        import json

        from agentrealm_agent.strategist import npc_lines

        w = world(["....."], at=(0, 0))
        w.entities = [gnawer((2, 0)), gnawer((3, 0), npc_id=10)]
        self.damaged(w, 9, 4)
        (line,) = [l for l in npc_lines(w, None) if l.startswith("nearby_npcs=")]
        rows = {r["id"]: r for r in json.loads(line.split("=", 1)[1])}
        self.assertEqual(rows[9]["health_left"], 6)
        self.assertNotIn("health_left", rows[10], "never hit: nothing to show")

    def test_health_left_is_at_least_one_and_a_boss_is_priced_as_before(self):
        self.assertEqual(health_left(gnawer((0, 0)), {9: 40}), 1)
        boss = Entity("npc", 3, (0, 0), "fake_boss", health=37, max_health=60)
        self.assertEqual(health_left(boss, {3: 5}), UNKILLED_HOSTILE_HEALTH, "a boss's pricing is A163's")

    def test_a_hurt_hostile_is_a_shorter_fight(self):
        e = gnawer((1, 0))
        self.assertAlmostEqual(
            ticks_to_kill_them([e], "pocket_knife", {9: 6}), ticks_to_kill_them([e], "pocket_knife") * 4 / 10
        )

    def test_the_engagement_counts_it(self):
        """A hostile the estimate would not fight fresh is fought once hurt enough."""
        w = world(["....."], at=(0, 0))
        w.health = 4
        w.armed_code = "pocket_knife"
        w.threat.record(("npc", CODE), 2)
        e = gnawer((1, 0))
        w.entities = [e]
        params = dict(PARAM_DEFAULTS)
        group = frozenset({("npc", 9)})
        fresh = decide(w, POLICY, params, group, False, m=Memory())
        self.damaged(w, 9, 8)
        hurt = decide(w, POLICY, params, group, False, m=Memory())
        self.assertGreater(hurt.ratio, fresh.ratio)
        self.assertAlmostEqual(hurt.ratio, win_ratio(4, [e], w.threat, "pocket_knife", 0, {9: 8}))
        self.assertFalse(fresh.fight)
        self.assertTrue(hurt.fight)


class SafeBlockCostsNothingTest(unittest.TestCase):
    def grid(self, w: WorldModel, params: CostGridParams | None = None) -> _Grid:
        return _Grid(w, {(0, 0)}, params or CostGridParams(is_hostile=lambda w, e: True))

    def test_lava_on_a_safe_block_costs_a_plain_step(self):
        w = world(["..ll"], at=(0, 0))
        safe(w, (2, 0))
        g = self.grid(w)
        self.assertEqual(g.cost((2, 0)), KNOWN_WALKABLE)
        self.assertEqual(g.cost((3, 0)), KNOWN_WALKABLE + COSTLY_STEP, "off the safe zone it still hurts")

    def test_a_hostile_beside_a_safe_block_adds_no_danger_there(self):
        w = world(["....."], at=(0, 0))
        safe(w, (3, 0))
        w.entities = [gnawer((4, 0))]
        g = self.grid(w)
        self.assertEqual(g.cost((3, 0)), KNOWN_WALKABLE)
        self.assertGreater(g.cost((2, 0)), KNOWN_WALKABLE)

    def test_priced_ground_on_a_safe_block_costs_nothing(self):
        w = world(["...."], at=(0, 0))
        safe(w, (2, 0))
        g = self.grid(w, CostGridParams(priced={(1, 0): 6, (2, 0): 6}))
        self.assertEqual((g.cost((1, 0)), g.cost((2, 0))), (KNOWN_WALKABLE + 6, KNOWN_WALKABLE))

    def test_a_hazard_on_a_safe_block_is_not_avoided(self):
        w = world(["..ll"], at=(0, 0))
        safe(w, (2, 0))
        blocked, _, _ = plan_sets(w, Memory(), POLICY, None)
        self.assertEqual(blocked, {(3, 0)})

    def test_a_safe_block_is_no_hostiles_ground(self):
        w = world(["." * 9], at=(0, 0))
        safe(w, (5, 0))
        w.entities = [gnawer((6, 0))]
        (g,) = walk_ground(w, POLICY).values()
        self.assertNotIn((5, 0), g.cells)
        self.assertIn((4, 0), g.cells)

    def test_a_pass_over_safe_blocks_counts_no_hits_there(self):
        """The corridor past a pocket the hostile steps up to the top of, 2
        from the corridor: within range of the walk there, so no pass; with
        those corridor cells safe, nothing hurts there and the pass is covered."""
        rows = list(ChaserApproachIsPricedTest.WALLED)
        rows[3] = "#" * 12 + "." + "#" * 10  # the pocket's top, (12, 3), 2 from the corridor

        def covers(safe_cells: list[tuple[int, int]]) -> bool:
            w = world(rows, at=(1, 1))
            w.max_health = w.health = 100
            w.threat.record(("npc", CODE), 2)
            w.entities = [gnawer((12, 4))]
            safe(w, *safe_cells)
            ground = walk_ground(w, POLICY)
            reach = {p for g in ground.values() for p in g.cells}
            return covers_pass(w, POLICY, dict(PARAM_DEFAULTS), ChaserApproachIsPricedTest.PATH, reach, 10, ground)

        self.assertFalse(covers([]))
        self.assertTrue(covers([(x, 1) for x in range(10, 15)]))

if __name__ == "__main__":
    unittest.main()
