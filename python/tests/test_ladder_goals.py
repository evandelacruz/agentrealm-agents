"""Lorraine's goal tree (A139): which node the walk picks, the one risk function, blocks and the dump."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.survival import expected_damage, ticks_to_kill_them
from agentrealm_agent.travel.knowledge import entrance_key, record_shop_cell
from agentrealm_agent.world import Entity, Sighting, WorldModel
from ladder import goals, readiness
from ladder.goals import ALIVE, GATHER, LEVEL, REQUIRES, RISK, Blocks, walk
from tests.test_starter_agent import FakeClient

MAP = 1
LEVEL_MAP = 9
DOOR = (10, 4)


def world(**kw) -> WorldModel:
    """A fresh character standing on the overworld with the starting sword."""
    w = WorldModel(character_id=1)
    w.map_id, w.pos, w.map_level = MAP, (5, 5), 0
    w.health = w.max_health = 10
    w.gems, w.armed_code, w.level_count = 0, "sword", 3
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def geared(**kw) -> WorldModel:
    """Strong enough for an unseen level: a big weapon, mail worn, large potions carried."""
    held = [InventorySupply(i, "large_potion") for i in (1, 2, 3)]
    kw = {"armed_code": "adamant_sword", "worn_codes": {"body": "adamant_mail"}, "held_supplies": held, **kw}
    return world(**kw)


def knowledge(*, door_need: str | None = None, sells: dict | None = None) -> KnowledgeBase:
    kb = KnowledgeBase.empty("sandbox")
    row = {"level": 1}
    if door_need:
        row["clue_needs"] = door_need
    kb.entrances[entrance_key(MAP, DOOR)] = row
    for i, (code, price) in enumerate((sells or {}).items()):
        record_shop_cell(kb, MAP, (20 + i, 20), code, price)
    return kb


def hostile(w: WorldModel, eid: int, pos, code="rat", map_id=MAP, in_view=True) -> Entity:
    e = Entity("npc", eid, pos, code)
    w.hostile_types.add(("npc", code))
    w.sightings[("npc", eid)] = Sighting(e, map_id, 0, pos, in_view=in_view)
    if in_view:
        w.entities.append(e)
    return e


class WalkTest(unittest.TestCase):
    def test_a_beaten_world_has_no_goal(self):
        turn = walk(world(levels_cleared=[1, 2, 3]), knowledge())
        self.assertIsNone(turn.goal)
        self.assertTrue(turn.checks["world"].ready)

    def test_geared_with_the_entrance_known_she_enters_the_next_level(self):
        w = geared()
        turn = walk(w, knowledge())
        self.assertIs(turn.goal, LEVEL)
        self.assertEqual(turn.op(w, knowledge()), {"op": "enter_level", "x": DOOR[0], "y": DOOR[1]})

    def test_the_target_is_the_lowest_level_not_cleared(self):
        kb = knowledge()
        kb.entrances[entrance_key(MAP, (2, 2))] = {"level": 2}
        w = geared(levels_cleared=[1])
        self.assertEqual(walk(w, kb).op(w, kb), {"op": "enter_level", "x": 2, "y": 2})

    def test_an_entrance_on_another_map_is_travelled_to(self):
        w = geared(map_id=7)
        self.assertEqual(walk(w, knowledge()).op(w, knowledge())["op"], "travel")

    def test_no_entrance_known_means_explore(self):
        w = geared()
        kb = KnowledgeBase.empty("sandbox")
        turn = walk(w, kb)
        self.assertIs(turn.goal, LEVEL)
        self.assertEqual(turn.op(w, kb)["op"], "explore_area")

    def test_a_need_with_gems_short_walks_down_to_gather(self):
        w = geared()
        kb = knowledge(door_need="fords", sells={"raft": 30})
        turn = walk(w, kb)
        self.assertIs(turn.goal, GATHER)
        self.assertEqual([n.key for n in turn.path], ["world", "level", "requires", "gems", "gather"])
        self.assertEqual(turn.op(w, kb), {"op": "gather_gems", "count": 30})

    def test_a_need_with_the_gems_held_is_bought(self):
        w = geared(gems=40)
        kb = knowledge(door_need="fords", sells={"raft": 30})
        turn = walk(w, kb)
        self.assertIs(turn.goal, REQUIRES)
        self.assertEqual(turn.op(w, kb), {"op": "buy", "code": "raft"})

    def test_a_need_lying_in_sight_is_fetched(self):
        w = geared(entities=[Entity("supply", 5, (6, 6), "raft")])
        kb = knowledge(door_need="fords")
        self.assertEqual(walk(w, kb).op(w, kb), {"op": "fetch_item", "code": "raft", "x": 6, "y": 6})

    def test_a_need_no_seller_sells_is_looked_for(self):
        w = geared()
        kb = knowledge(door_need="fords")
        turn = walk(w, kb)
        self.assertIs(turn.goal, REQUIRES)
        self.assertEqual(turn.op(w, kb)["op"], "explore_area")
        self.assertIn("no seller known for fords", turn.checks["gems"].why)

    def test_a_need_met_by_what_is_worn_holds(self):
        w = geared(worn_codes={"body": "adamant_mail", "feet": "raft"})
        self.assertTrue(walk(w, knowledge(door_need="fords")).checks["requires"].ready)

    def test_short_of_risk_with_the_gems_she_buys_what_closes_it(self):
        w = world(gems=500)
        kb = knowledge(sells={"large_potion": 20, "adamant_sword": 200, "adamant_mail": 150})
        turn = walk(w, kb)
        self.assertIs(turn.goal, RISK)
        self.assertEqual(turn.op(w, kb)["op"], "buy")

    def test_short_of_risk_she_first_arms_better_gear_she_holds(self):
        w = world(held_supplies=[InventorySupply(1, "adamant_sword")])
        self.assertEqual(walk(w, knowledge()).op(w, knowledge()), {"op": "equip"})

    def test_short_of_risk_with_gems_short_she_gathers_for_both_branches(self):
        w = world()
        kb = knowledge(door_need="fords", sells={"raft": 30, "large_potion": 20})
        m = readiness.missing(w, kb, LEVEL)
        self.assertEqual(m.buys[0].code, "raft")
        self.assertIn("large_potion", [p.code for p in m.buys])
        turn = walk(w, kb)
        self.assertIs(turn.goal, GATHER)
        self.assertEqual(turn.op(w, kb), {"op": "gather_gems", "count": m.gems})

    def test_a_hostile_in_reach_she_cannot_afford_is_stay_alive(self):
        w = world(health=2)
        hostile(w, 50, (6, 5))
        kb = knowledge(door_need="fords", sells={"raft": 30})
        turn = walk(w, kb)
        self.assertIs(turn.goal, ALIVE)
        self.assertEqual(turn.op(w, kb), {"op": "travel", "to": "town"})

    def test_stay_alive_eats_food_in_sight_first(self):
        w = world(health=2)
        hostile(w, 50, (6, 5))
        w.entities.append(Entity("supply", 6, (4, 4), "golden_cap"))
        kb = knowledge(door_need="fords", sells={"raft": 30})
        self.assertEqual(walk(w, kb).op(w, kb), {"op": "fetch_item", "code": "golden_cap", "x": 4, "y": 4})

    def test_nothing_in_reach_she_gathers_at_any_health(self):
        w = world(health=2)
        hostile(w, 50, (40, 40))
        turn = walk(w, knowledge(door_need="fords", sells={"raft": 30}))
        self.assertIs(turn.goal, GATHER)


class RiskBudgetTest(unittest.TestCase):
    def test_the_cost_is_garys_win_estimate(self):
        w = world()
        foe = hostile(w, 50, (6, 5))
        b = readiness.risk_budget(GATHER, w, None)
        want = expected_damage([foe], w.threat, ticks_to_kill_them([foe], "sword"))
        self.assertAlmostEqual(b.cost, want)

    def test_armor_worn_lowers_the_cost_and_armor_held_does_not(self):
        w = world()
        hostile(w, 50, (6, 5))
        bare = readiness.risk_budget(GATHER, w, None).cost
        w.held_supplies = [InventorySupply(1, "adamant_mail")]
        self.assertEqual(readiness.risk_budget(GATHER, w, None).cost, bare)
        w.worn_codes = {"body": "adamant_mail"}
        self.assertLess(readiness.risk_budget(GATHER, w, None).cost, bare)

    def test_held_heals_count_beside_health(self):
        w = world(held_supplies=[InventorySupply(1, "potion")])
        self.assertEqual(readiness.risk_budget(GATHER, w, None).have, 20)

    def test_a_valuable_goal_may_cost_more_than_an_ordinary_one(self):
        w = world()
        ordinary = readiness.Budget(10, 0, 6.0, readiness.ORDINARY)
        valuable = readiness.Budget(10, 0, 6.0, readiness.VALUABLE)
        self.assertFalse(ordinary.ok)
        self.assertTrue(valuable.ok)
        self.assertEqual(LEVEL.value, readiness.VALUABLE)
        self.assertEqual(GATHER.value, readiness.ORDINARY)
        self.assertEqual(readiness.risk_budget(GATHER, w, None).cost, 0)

    def test_an_unseen_level_is_priced_by_its_prior(self):
        foes = readiness.level_foes(world(), knowledge())
        self.assertEqual(len(foes), readiness.LEVEL_PRIOR_FOES)

    def test_hostiles_seen_in_the_level_replace_the_prior(self):
        w = world(map_levels={LEVEL_MAP: 1})
        hostile(w, 60, (3, 3), code="bogmaw", map_id=LEVEL_MAP, in_view=False)
        self.assertEqual([e.code for e in readiness.level_foes(w, knowledge())], ["bogmaw"])

    def test_a_boss_seen_is_priced_by_its_health(self):
        w = world()
        weak = Entity("npc", 1, (0, 0), "rat")
        boss = Entity("npc", 2, (0, 0), "rat", health=50, max_health=50)
        lo = readiness.Loadout.now(w)
        self.assertAlmostEqual(readiness.fight_cost(boss, w, lo), 5 * readiness.fight_cost(weak, w, lo))

    def test_the_reflex_hook_reads_the_same_function(self):
        w = world()
        hostile(w, 50, (6, 5))
        self.assertEqual(readiness.margin_now(GATHER, w, None), readiness.risk_budget(GATHER, w, None))


class BlocksTest(unittest.TestCase):
    def test_a_blocked_goal_is_skipped_with_its_reason_and_the_walk_descends(self):
        w = geared()
        blocks = Blocks()
        blocks.mark("level", "the door will not open", now=w.tick)
        turn = walk(w, knowledge(), blocks)
        self.assertIs(turn.goal, RISK)
        self.assertEqual([(n.key, b.reason) for n, b in turn.skipped], [("level", "the door will not open")])

    def test_a_blocked_node_on_the_way_down_is_passed_through(self):
        w = geared()
        kb = knowledge(door_need="fords", sells={"raft": 30})
        blocks = Blocks()
        blocks.mark("gather", "no ground to gather on", now=w.tick)
        turn = walk(w, kb, blocks)
        self.assertIs(turn.goal, ALIVE)
        self.assertEqual(turn.skipped[0][0], GATHER)

    def test_a_block_lapses_at_its_expiry(self):
        w = geared()
        blocks = Blocks()
        blocks.mark("level", "the door will not open", now=0, ticks=100)
        w.tick = 100
        self.assertIs(walk(w, knowledge(), blocks).goal, LEVEL)
        self.assertNotIn("level", blocks)

    def test_a_block_can_be_lifted(self):
        w = geared()
        blocks = Blocks()
        blocks.mark("level", "the door will not open", now=0)
        blocks.lift("level")
        self.assertIs(walk(w, knowledge(), blocks).goal, LEVEL)

    def test_every_way_down_blocked_leaves_no_goal(self):
        w = world(levels_cleared=[])
        kb = knowledge(door_need="fords", sells={"raft": 30})
        blocks = Blocks()
        for key in ("level", "requires", "risk", "gems", "gather", "alive"):
            blocks.mark(key, "cannot", now=0)
        self.assertIsNone(walk(w, kb, blocks).goal)


class DumpTest(unittest.TestCase):
    def test_the_dump_shows_every_node_once_and_the_goal(self):
        w = geared()
        kb = knowledge(door_need="fords", sells={"raft": 30})
        lines = goals.dump(w, kb)
        self.assertTrue(lines[0].startswith("beat the world: not ready (level 1 of 3 not cleared)"))
        self.assertIn("    hold what it requires: not ready (its entrance needs fords)", lines)
        self.assertIn("      hold the gems for what's missing: as above", lines)
        self.assertEqual(lines[-1], 'goal: gather op={"count": 30, "op": "gather_gems"}')

    def test_a_blocked_node_shows_its_reason(self):
        blocks = Blocks()
        blocks.mark("level", "the door will not open", now=0)
        lines = goals.dump(geared(), knowledge(), blocks)
        self.assertIn("blocked: the door will not open", lines[1])


class StatusTest(unittest.TestCase):
    def test_status_prints_the_tree(self):
        from ladder.__main__ import status

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)), mock.patch(
            "agentrealm_agent.knowledge_base.WORLDS_DIR", Path(tmp) / "worlds"
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(status(FakeClient(), CharacterConfig("T", "sandbox", Policy(), Path("t.toml")), 42), 0)
        text = out.getvalue()
        self.assertIn("beat the world: not ready", text)
        self.assertIn("goal: ", text)


if __name__ == "__main__":
    unittest.main()
