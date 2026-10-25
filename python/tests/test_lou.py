"""Layered Lou (docs/v3/SPEC.md) on made-up worlds: the needs graph, the engines, memory and the strategist's brief.

Each test builds a small invented map, puts Lou on it and checks what she
decides. No test reaches the network or ``python/.state``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentrealm_adk.memory import Memory as NavMemory
from agentrealm_adk.world import Entity, MapView, WorldModel

from adk3.core.loop import Briefing
from adk3.core.needs import Graph, Status
from adk3.realm import actions, harm
from adk3.realm.context import Ctx
from adk3.realm.memory import Clue, Death, Memory
from adk3.realm.state import State
from adk3.realm.value import Levers
from lou import engines, needs, opportunities
from lou.orders import GoTo
from lou.registry import ROOT
from lou.strategist import adopt, describe

MAP = 1


def world(rows: list[str], at=(5, 5), health=10, gems=0, armed="pocket_knife", safe=()) -> WorldModel:
    """A map from rows of letters: g grass, d dirt, b bush, t tree, w wall."""
    names = {"g": "grass", "d": "dirt", "b": "bush", "t": "tree", "w": "wall"}
    w = WorldModel(42)
    w.map_id, w.map_level, w.pos, w.tick = MAP, 0, at, 1000
    w.health, w.max_health, w.gems, w.lives = health, 10, gems, 9
    w.armed_code, w.armed_id = armed, 1
    w.attack_range = 1
    w.perception = 12
    w.level_count = 8
    view = MapView()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            view.tiles[(x, y)] = names[ch]
    view.safe = set(safe)
    w.maps[MAP] = view
    return w


def ctx_of(w: WorldModel, memory: Memory | None = None, levers: Levers | None = None, **judgments) -> Ctx:
    s = State.of(w, 10, None, 8, None, 4)
    return Ctx(s, memory or Memory(), None, NavMemory(), (MAP, (1, 1)), levers or Levers(), dict(judgments), {})


OPEN = ["g" * 11] * 11


class NeedsGraphTest(unittest.TestCase):
    def test_no_entrances_known_looks_for_them(self):
        ctx = ctx_of(world(OPEN))
        path = Graph(ROOT).focus(ctx, 0)
        self.assertEqual(path[-1].key, "know:entrances")

    def test_a_level_with_no_judgment_waits_on_the_strategist_and_lou_keeps_learning(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        ctx = ctx_of(world(OPEN), mem)
        path = Graph(ROOT).focus(ctx, 0)
        self.assertEqual(path[-1].key, "know:world")
        self.assertIs(needs.ReadyFor("1:9,9").check(ctx).status, Status.UNKNOWN)

    def test_gear_judged_and_seen_for_sale_but_too_dear_means_earning_gems(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        mem.saw_for_sale("bronze_sword", 15, MAP, (2, 2))
        ctx = ctx_of(world(OPEN, gems=3), mem, needs={"1:9,9": {"items": ["bronze_sword"]}})
        path = Graph(ROOT).focus(ctx, 0)
        self.assertEqual([n.key for n in path][-3:], ["ready:1:9,9", "have:bronze_sword", "gems:15"])

    def test_with_the_gems_she_buys_it(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        mem.saw_for_sale("bronze_sword", 15, MAP, (2, 2))
        ctx = ctx_of(world(OPEN, gems=20), mem, needs={"1:9,9": {"items": ["bronze_sword"]}})
        path = Graph(ROOT).focus(ctx, 0)
        self.assertEqual(path[-1].key, "have:bronze_sword")
        self.assertEqual(path[-1].skill(ctx).name, "buy")

    def test_never_seen_for_sale_means_finding_the_seller(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        ctx = ctx_of(world(OPEN), mem, needs={"1:9,9": {"items": ["iron_sword"]}})
        self.assertEqual(Graph(ROOT).focus(ctx, 0)[-1].key, "know:seller:iron_sword")

    def test_a_blocked_need_lifts_when_something_new_is_known(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        ctx = ctx_of(world(OPEN), mem, needs={"1:9,9": {"items": []}})
        graph = Graph(ROOT)
        reach = graph.focus(ctx, 5)[-1]
        self.assertEqual(reach.key, "reach:1:9,9")
        graph.block(reach, "no known way", 5)
        self.assertNotEqual(graph.focus(ctx, 5)[-1].key, "reach:1:9,9")
        self.assertEqual(graph.focus(ctx, 6)[-1].key, "reach:1:9,9")


class CombatTest(unittest.TestCase):
    def snotlings(self, w: WorldModel, cells) -> None:
        w.entities = [Entity("npc", 100 + i, p, "snotling") for i, p in enumerate(cells)]
        w.hostile_types.add(("npc", "snotling"))

    def test_one_snotling_beside_a_healthy_lou_is_fought(self):
        w = world(OPEN)
        self.snotlings(w, [(6, 5)])
        ctx = ctx_of(w)
        choice = engines.Combat().choose(ctx, None)
        self.assertTrue(choice.name.startswith("fight"), choice.name)

    def test_a_pack_against_a_hurt_lou_is_not_fought(self):
        w = world(OPEN, health=2, safe={(0, 0), (1, 0), (0, 1)})
        self.snotlings(w, [(6, 5), (6, 6), (5, 6)])
        choice = engines.Combat().choose(ctx_of(w), None)
        self.assertFalse(choice.name.startswith("fight"), choice.name)

    def test_the_odds_count_deaths(self):
        w = world(OPEN, health=1)
        self.snotlings(w, [(6, 5), (6, 6), (5, 6), (4, 6)])
        self.assertGreater(harm.odds(ctx_of(w).state, harm.Course()).death, 0.5)

    def test_a_wall_stops_a_chaser(self):
        rows = ["ggggggggggg"] * 5 + ["wwwwwwwwwww"] + ["ggggggggggg"] * 5
        w = world(rows, at=(5, 3))
        self.snotlings(w, [(5, 8)])
        self.assertEqual(harm.odds(ctx_of(w).state, harm.Course()).hits_taken, 0)


class TravelTest(unittest.TestCase):
    def test_a_bush_in_the_way_is_cut(self):
        rows = ["wwwwwwwwwww", "wwwwwwwwwww", "ggggbgggggg", "wwwwwwwwwww"]
        ctx = ctx_of(world(rows, at=(3, 2)))
        action = engines.Travel().act(ctx, GoTo(MAP, (10, 2), "east"))
        self.assertIsInstance(action, actions.UseOn)
        self.assertEqual((action.x, action.y), (4, 2))

    def test_a_bush_a_strike_left_standing_is_a_wall(self):
        rows = ["wwwwwwwwwww", "ggggbgggggg", "ggggggggggg"]
        mem = Memory()
        mem.changed_nothing(MAP, (4, 1))
        ctx = ctx_of(world(rows, at=(3, 1)), mem)
        action = engines.Travel().act(ctx, GoTo(MAP, (10, 1), "east"))
        self.assertIsInstance(action, actions.Walk)
        self.assertNotIn((4, 1), action.path)


class OpportunitiesTest(unittest.TestCase):
    def test_a_gem_beside_her_is_taken(self):
        w = world(OPEN)
        w.entities = [Entity("supply", 7, (6, 5), "gem")]
        self.assertIsInstance(engines.Opportunities().pick(ctx_of(w)), actions.Take)

    def test_an_unread_sign_is_read_and_a_read_one_is_not(self):
        w = world(OPEN)
        w.view.readable[(7, 5)] = True
        ctx = ctx_of(w)
        self.assertIsInstance(engines.Opportunities().pick(ctx), actions.Read)
        ctx.memory.read_sign(MAP, (7, 5))
        self.assertIsNone(engines.Opportunities().pick(ctx))

    def test_food_waits_until_she_is_hurt_by_its_worth(self):
        w = world(OPEN, health=10)
        w.entities = [Entity("supply", 8, (6, 5), "apple")]
        self.assertFalse([c for c in opportunities.Food().find(ctx_of(w))])
        w.health = 6
        self.assertTrue([c for c in opportunities.Food().find(ctx_of(w))])

    def test_a_helper_standing_still_is_greeted_from_afar(self):
        w = world(OPEN)
        w.entities = [Entity("npc", 9, (9, 9), "town_helper")]
        w.npc_still[9] = ((9, 9), w.tick - 200)
        self.assertIsInstance(engines.Opportunities().pick(ctx_of(w)), actions.Say)


class MemoryTest(unittest.TestCase):
    def test_it_keeps_everything_across_runs(self):
        path = Path(tempfile.mkdtemp()) / "m.json"
        mem = Memory(path)
        mem.add_clue(Clue("the door lies east of the well", "helper", "town_helper", MAP, 3, 3))
        mem.entrance(MAP, (9, 9)).locked = True
        mem.saw_for_sale("bronze_sword", 15, MAP, (2, 2))
        mem.died(Death(1.0, MAP, 4, 4, "snotling", 77, MAP, 4, 5, ["bronze_sword"]))
        mem.save()
        back = Memory.load(path)
        self.assertEqual(back.clues[0].text, "the door lies east of the well")
        self.assertTrue(back.entrances["1:9,9"].locked)
        self.assertEqual(back.sellers("bronze_sword")[0].price, 15)
        self.assertEqual(back.last_death().items, ["bronze_sword"])

    def test_a_clue_is_kept_once(self):
        mem = Memory()
        self.assertTrue(mem.add_clue(Clue("x", "sign", "", MAP, 1, 1)))
        self.assertFalse(mem.add_clue(Clue("x", "sign", "", MAP, 1, 1)))


class StrategistBriefTest(unittest.TestCase):
    def test_the_brief_carries_clues_entrances_and_the_graph(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        mem.add_clue(Clue("seek the door by the old oak", "helper", "town_helper", MAP, 3, 3))
        ctx = ctx_of(world(OPEN), mem)
        graph = Graph(ROOT)
        path = graph.focus(ctx, 0)
        text = describe(Briefing(ctx, "starting", graph.rows(ctx, path), [], [], 0.0))
        self.assertIn("seek the door by the old oak", text)
        self.assertIn("1:9,9", text)
        self.assertIn("beat the world", text)

    def test_an_answer_sets_judgments_and_tactics(self):
        ctx = ctx_of(world(OPEN))
        adopt(ctx, {"order": ["1:9,9"], "needs": {"1:9,9": {"items": ["bronze_sword"]}}, "tactics": {"avoid": ["chugbug"]}})
        self.assertEqual(ctx.judgments["order"], ["1:9,9"])
        self.assertEqual(ctx.tactics["avoid"], ["chugbug"])


class LeversTest(unittest.TestCase):
    def test_a_reckless_agent_weighs_death_lightly(self):
        self.assertLess(Levers(risk=10).death_weight, Levers(risk=5).death_weight)
        self.assertLess(Levers(risk=5).death_weight, Levers(risk=0).death_weight)


if __name__ == "__main__":
    unittest.main()
