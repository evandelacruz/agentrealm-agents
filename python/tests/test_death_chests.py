"""A122: every death chest of ours is remembered, shown to the planner, and taken back."""

import json
import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_base.death_chests import (
    DEATH_CHESTS_GONE_KEY,
    DEATH_CHESTS_KEY,
    GONE_KEPT,
    load_death_chests,
    save_death_chests,
)
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.strategist import DEATH_CHESTS_SHOWN, death_chests_line
from agentrealm_agent.world import WorldModel
from agentrealm_agent.zone_discovery import apply_zone

MAP = 7


def world(at=(10, 0), perception=5) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=perception)
    for x in range(30):
        w.view.tiles[(x, 0)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    return w


def die(w: WorldModel, tick: int, chest_id: int, x: int, *, map_id=MAP) -> None:
    """Die at ``x``, then stand back where we were (respawned)."""
    here = w.pos
    w.tick = tick
    w.apply_events([{"tick": tick, "events": [{"kind": "Died", "cause": "killed", "chest_id": chest_id, "map_id": map_id, "x": x, "y": 0}]}])
    w.map_id, w.pos = MAP, here


def reloaded(kb: KnowledgeBase) -> KnowledgeBase:
    """``kb`` written to JSON and read back, as across runs."""
    return KnowledgeBase.from_dict(kb.world_code, json.loads(json.dumps(kb.to_dict())))


def ctx() -> PlayContext:
    policy = Policy(kind="scripted", goals=[], pickup=True)
    return PlayContext(Memory(), policy, random.Random(0), plan=Plan.from_policy(policy, dict(PARAM_DEFAULTS)))


class RememberedTest(unittest.TestCase):
    def test_two_deaths_keep_two_chests(self):
        w = world()
        die(w, 5, 80, 0)
        die(w, 9, 81, 20)
        self.assertEqual(sorted(w.death_chests), [80, 81])
        self.assertEqual(w.newest_death_chest()[0], 81)

    def test_served_death_chest_is_remembered(self):
        w = world()
        w.apply_observation({"complete": True, "snapshot": {"death_chest": {"chest_id": 82, "map_id": 9, "x": 3, "y": 4}}})
        self.assertEqual((w.death_chests[82].map_id, w.death_chests[82].pos), (9, (3, 4)))

    def test_contents_are_kept_once_seen(self):
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [
            {"id": 80, "x": 0, "y": 0, "contents": [{"id": 1, "supply_subtype_code": "bronze_sword"}]},
        ]}}})
        w.pos = (10, 0)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": []}}})
        self.assertEqual(w.death_chests[80].contents, ["bronze_sword"])

    def test_an_empty_chest_is_dropped(self):
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        die(w, 9, 81, 20)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [{"id": 80, "x": 0, "y": 0, "contents": []}]}}})
        self.assertEqual(sorted(w.death_chests), [81])

    def test_a_chest_gone_from_reach_is_dropped(self):
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": []}}})
        self.assertEqual(w.death_chests, {})

    def test_a_chest_out_of_reach_missing_from_sight_is_kept(self):
        w = world(at=(3, 0))
        die(w, 5, 80, 0)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": []}}})
        self.assertEqual(sorted(w.death_chests), [80])

    def test_a_chest_seen_gone_is_not_remembered_again_when_served(self):
        w = world(at=(1, 0))
        served = {"death_chest": {"chest_id": 80, "map_id": MAP, "x": 0, "y": 0}}
        w.apply_observation({"complete": True, "snapshot": {**served, "entities": {"chests": [{"id": 80, "x": 0, "y": 0, "contents": []}]}}})
        self.assertEqual(w.death_chests, {})
        w.apply_observation({"complete": True, "snapshot": {**served, "entities": {"chests": []}}})
        self.assertEqual(w.death_chests, {})


class PersistTest(unittest.TestCase):
    def test_chests_persist_across_a_reload(self):
        kb = KnowledgeBase("sandbox")
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        die(w, 9, 81, 20, map_id=9)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [
            {"id": 80, "x": 0, "y": 0, "contents": [{"id": 1, "supply_subtype_code": "apple"}]},
        ]}}})
        save_death_chests(kb, w)

        fresh = world()
        load_death_chests(reloaded(kb), fresh)
        self.assertEqual(sorted(fresh.death_chests), [80, 81])
        self.assertEqual(fresh.death_chests[80].contents, ["apple"])
        self.assertIsNone(fresh.death_chests[81].contents)
        self.assertEqual((fresh.death_chests[81].map_id, fresh.death_chests[81].pos), (9, (20, 0)))
        self.assertEqual(fresh.newest_death_chest()[0], 81)

    def test_another_characters_chests_are_not_loaded_or_overwritten(self):
        kb = KnowledgeBase("sandbox")
        kb.extra[DEATH_CHESTS_KEY] = {"2": {"90": {"map_id": MAP, "x": 3, "y": 0, "died": 1}}}
        w = world()
        load_death_chests(kb, w)
        self.assertEqual(w.death_chests, {})
        die(w, 5, 80, 0)
        save_death_chests(kb, w)
        self.assertEqual(sorted(kb.extra[DEATH_CHESTS_KEY]), ["1", "2"])

    def test_a_row_that_does_not_parse_is_skipped(self):
        kb = KnowledgeBase("sandbox")
        kb.extra[DEATH_CHESTS_KEY] = {"1": {
            "80": {"map_id": MAP, "x": 0, "y": 0, "died": 5},
            "81": {"map_id": MAP, "x": "far"},
            "x": {"map_id": MAP, "x": 1, "y": 0},
            "82": "not a row",
        }}
        w = world()
        load_death_chests(kb, w)
        self.assertEqual(sorted(w.death_chests), [80])

    def test_an_emptied_chest_leaves_the_file(self):
        kb = KnowledgeBase("sandbox")
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        save_death_chests(kb, w)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [{"id": 80, "x": 0, "y": 0, "contents": []}]}}})
        save_death_chests(kb, w)
        self.assertNotIn(DEATH_CHESTS_KEY, kb.extra)

    def test_a_chest_seen_gone_stays_gone_after_a_reload(self):
        kb = KnowledgeBase("sandbox")
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [{"id": 80, "x": 0, "y": 0, "contents": []}]}}})
        save_death_chests(kb, w)

        fresh = world()
        load_death_chests(reloaded(kb), fresh)
        fresh.apply_observation({"complete": True, "snapshot": {"death_chest": {"chest_id": 80, "map_id": MAP, "x": 0, "y": 0}}})
        self.assertEqual(fresh.death_chests, {})

    def test_only_the_latest_gone_ids_are_kept(self):
        kb = KnowledgeBase("sandbox")
        w = world()
        w.death_chests_gone = list(range(1, GONE_KEPT + 3))
        save_death_chests(kb, w)
        self.assertEqual(kb.extra[DEATH_CHESTS_GONE_KEY]["1"], list(range(3, GONE_KEPT + 3)))


class StateLineTest(unittest.TestCase):
    def test_none_known(self):
        self.assertEqual(death_chests_line(world()), "death_chests=none known")

    def test_capped_with_a_count_of_the_rest(self):
        w = world()
        for i in range(DEATH_CHESTS_SHOWN + 2):
            die(w, i + 1, 100 + i, i)
        line = death_chests_line(w)
        self.assertTrue(line.endswith(" (+2 more)"), line)
        rows = json.loads(line.removeprefix("death_chests=").removesuffix(" (+2 more)"))
        self.assertEqual(len(rows), DEATH_CHESTS_SHOWN)
        self.assertEqual(rows[0]["chest_id"], 110 - 1)  # cell 9, nearest to 10

    def test_each_chest_nearest_first(self):
        w = world()
        die(w, 5, 80, 0)
        die(w, 6, 81, 13)
        die(w, 7, 82, 4, map_id=9)
        w.death_chests[81].contents = ["bronze_sword"]
        rows = json.loads(death_chests_line(w).removeprefix("death_chests="))
        self.assertEqual(
            rows,
            [
                {"chest_id": 81, "cell": "7:13,0 (3 cells east)", "contents": ["bronze_sword"], "in_view": True},
                {"chest_id": 80, "cell": "7:0,0 (10 cells west)", "contents": "unknown", "in_view": False},
                {"chest_id": 82, "cell": "9:4,0 (another map)", "contents": "unknown", "in_view": False},
            ],
        )


class RecoverRememberedTest(unittest.TestCase):
    def test_an_older_chest_in_view_on_a_safe_spot_is_walked_to(self):
        w = world()
        die(w, 5, 80, 13)  # older, 3 cells east, in view
        die(w, 9, 81, 29)  # newest, no safe spot known
        apply_zone(w, MAP, 13, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Recover")
        self.assertEqual((out.intents[0]["verb"], out.intents[0]["x"]), ("SetPosition", 11))

    def test_an_older_chest_out_of_view_is_the_planners(self):
        w = world()
        die(w, 5, 80, 0)  # older, 10 cells west, out of view
        die(w, 9, 81, 29)
        apply_zone(w, MAP, 0, 0, {"safe": True, "brightness": 1})
        self.assertNotEqual(dispatch(w, ctx()).state, "Recover")

    def test_a_chest_in_reach_with_nothing_to_take_does_not_hide_the_others(self):
        w = world(at=(1, 0))
        die(w, 5, 80, 0)  # in reach, but the pack is full
        die(w, 9, 81, 4)  # newest, on a safe spot
        w.held_supplies = [InventorySupply(i, "torch") for i in range(1, w.carry_capacity + 1)]
        w.chest_contents[80] = [InventorySupply(90, "bronze_sword")]
        apply_zone(w, MAP, 4, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Recover")
        self.assertEqual((out.intents[0]["verb"], out.intents[0]["x"]), ("SetPosition", 2))

    def test_a_remembered_chest_in_reach_is_taken_back_off_safe_ground(self):
        w = world(at=(1, 0))
        die(w, 5, 80, 0)
        die(w, 9, 81, 29)
        w.chest_contents[80] = [InventorySupply(1, "bronze_sword")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Recover")
        self.assertEqual(out.intents, [{"verb": "WithdrawFromChest", "chest_id": 80}])


if __name__ == "__main__":
    unittest.main()
