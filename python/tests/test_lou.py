"""Layered Lou (docs/v3/SPEC.md) on made-up worlds: the publication and the executive, the reactions, memory and the strategist's brief.

Each test builds a small invented map, puts Lou on it and checks what she
decides. No test reaches the network or ``python/.state``.
"""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.memory import Memory as NavMemory
from agentrealm_adk.world import Entity, MapView, WorldModel

from adk3.core.executive import Executive
from adk3.core.loop import Briefing
from adk3.realm import actions, harm
from adk3.realm.context import Ctx
from adk3.realm.memory import Clue, Death, Memory
from adk3.realm.state import State
from adk3.realm.value import Levers
from lou import engines, opportunities, publish, skills
from lou.agent import Standing
from lou.orders import GoTo, Work
from lou.strategist import describe

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


def ctx_of(w: WorldModel, memory: Memory | None = None, levers: Levers | None = None, **tactics) -> Ctx:
    s = State.of(w, 10, None, 8, None, 4)
    return Ctx(s, memory or Memory(), None, NavMemory(), (MAP, (1, 1)), levers or Levers(), dict(tactics))


OPEN = ["g" * 11] * 11


def fallback(ctx):
    return Standing("safe work"), publish.Approach("cut grass nearby", "earn_gems", {}, {}, {})


GEMS_15 = {"goal": {"test": "gems", "n": 15}, "approach": {"method": "earn_gems", "x": 5, "y": 5}}


class PublicationTest(unittest.TestCase):
    def test_an_answer_becomes_goal_approach_plan_b_and_next(self):
        mem = Memory()
        mem.saw_for_sale("bronze_sword", 15, MAP, (2, 2))
        answer = {**GEMS_15, "plan_b": {"method": "earn_gems", "x": 1, "y": 1},
                  "next": {"goal": {"test": "carry", "item": "bronze_sword"}, "approach": {"method": "buy", "item": "bronze_sword"}}}
        p = publish.publication(answer, ctx_of(world(OPEN), mem))
        self.assertEqual((p.goal.test, p.approach.method, p.plan_b.method, p.next.approach.method),
                         ("gems", "earn_gems", "earn_gems", "buy"))

    def test_an_unknown_method_is_refused_with_the_menu(self):
        with self.assertRaisesRegex(ValueError, "earn_gems"):
            publish.publication({"goal": {"test": "gems", "n": 1}, "approach": {"method": "farm"}}, ctx_of(world(OPEN)))

    def test_plan_a_that_cannot_start_is_refused_with_why(self):
        answer = {"goal": {"test": "carry", "item": "bronze_sword"}, "approach": {"method": "buy", "item": "bronze_sword"}}
        with self.assertRaisesRegex(ValueError, "no shelf selling bronze_sword"):
            publish.publication(answer, ctx_of(world(OPEN)))


class ExecutiveTest(unittest.TestCase):
    def test_nothing_published_runs_the_fallback(self):
        ex = Executive(fallback)
        self.assertIsInstance(ex.step(ctx_of(world(OPEN)), 0.0), skills.EarnGems)
        self.assertEqual(ex.plan, "fallback")

    def test_the_goal_met_starts_next_at_once(self):
        mem = Memory()
        mem.saw_for_sale("bronze_sword", 15, MAP, (2, 2))
        answer = {**GEMS_15, "next": {"goal": {"test": "carry", "item": "bronze_sword"},
                                      "approach": {"method": "buy", "item": "bronze_sword"}}}
        ex = Executive(fallback)
        ctx = ctx_of(world(OPEN, gems=3), mem)
        ex.adopt(publish.publication(answer, ctx), ctx, 0.0)
        ex.step(ctx, 1.0)
        rich = ctx_of(world(OPEN, gems=20), mem)
        self.assertIsInstance(ex.step(rich, 2.0), skills.Buy)
        self.assertEqual(ex.reports[-1].result, "done")

    def test_a_failed_approach_switches_to_plan_b_then_falls_back(self):
        answer = {**GEMS_15, "approach": {**GEMS_15["approach"], "fails_if": {"minutes": 1}},
                  "plan_b": {"method": "forage", "fails_if": {"minutes": 1}}}
        ex = Executive(fallback)
        ctx = ctx_of(world(OPEN))
        ex.adopt(publish.publication(answer, ctx), ctx, 0.0)
        self.assertIsInstance(ex.step(ctx, 61.0), skills.Forage)
        self.assertEqual((ex.plan, ex.reports[-1].why), ("B", "its 1 minutes ran out"))
        ex.step(ctx, 200.0)
        self.assertEqual(ex.plan, "fallback")

    def test_no_progress_for_the_patience_fails_the_approach(self):
        ex = Executive(fallback)
        ctx = ctx_of(world(OPEN), levers=Levers(patience=30))
        ex.adopt(publish.publication({"goal": {"test": "health", "n": 99}, "approach": {"method": "forage"}}, ctx), ctx, 0.0)
        ex.step(ctx, 0.0)
        ex.step(ctx, 31.0)
        self.assertEqual([r.why for r in ex.reports[-2:]], ["no progress for 30s", "no plan B"])


class StayAliveTest(unittest.TestCase):
    def test_hurt_with_food_in_view_she_eats(self):
        w = world(OPEN, health=3)
        w.entities = [Entity("supply", 5, (8, 8), "apple")]
        ctx = ctx_of(w)
        self.assertIsInstance(skills.Forage().order(ctx).action, actions.Take)

    def test_hurt_with_no_food_she_cuts_bushes_for_berries_not_stands_in_town(self):
        rows = ["ggggggggggg"] * 5 + ["gggbggggggg"] + ["ggggggggggg"] * 5
        ctx = ctx_of(world(rows, health=3))
        order = skills.Forage().order(ctx)
        self.assertIsInstance(order, Work)
        self.assertIn((3, 5), order.cells)


class FieldTest(unittest.TestCase):
    def test_grass_a_snotling_would_reach_first_is_not_worked(self):
        w = world(["g" * 21] * 11, at=(10, 5))
        w.entities = [Entity("npc", 101, (2, 5), "snotling")]
        w.hostile_types.add(("npc", "snotling"))
        cells = skills.EarnGems().cells(ctx_of(w))
        self.assertTrue(cells)
        self.assertTrue(all(c[0] > 6 for c in cells), cells)

    def test_the_strategists_field_is_where_she_works(self):
        w = world(["g" * 41] * 11, at=(5, 5))
        order = skills.EarnGems((35, 5)).order(ctx_of(w))
        self.assertIsInstance(order, Work)
        self.assertTrue(all(abs(x - 35) <= skills.FIELD_SEARCH for x, _ in order.cells), order.cells)


class CombatTest(unittest.TestCase):
    def snotlings(self, w: WorldModel, cells) -> None:
        w.entities = [Entity("npc", 100 + i, p, "snotling") for i, p in enumerate(cells)]
        w.hostile_types.add(("npc", "snotling"))

    def plan(self, ctx, order=None):
        return engines.Combat().plan(ctx, order, harm.threats_near(ctx.state))

    def test_one_snotling_beside_a_healthy_lou_is_fought(self):
        w = world(OPEN)
        self.snotlings(w, [(6, 5)])
        self.assertEqual(self.plan(ctx_of(w)).kind, "fight")

    def test_a_pack_against_a_hurt_lou_is_not_fought(self):
        w = world(OPEN, health=2, safe={(0, 0), (1, 0), (0, 1)})
        self.snotlings(w, [(6, 5), (6, 6), (5, 6)])
        self.assertNotEqual(self.plan(ctx_of(w)).kind, "fight")

    def test_an_engagement_is_kept_until_it_ends(self):
        w = world(OPEN)
        self.snotlings(w, [(6, 5)])
        combat = engines.Combat()
        combat.applies(ctx_of(w), None)
        first = combat.now
        w.tick += 1
        w.health = 6
        combat.applies(ctx_of(w), None)
        self.assertIs(combat.now, first)

    def test_held_up_on_safe_ground_she_holds_and_the_approach_fails_to_plan_b(self):
        w = world(OPEN, health=3, safe={(5, 5), (4, 5), (4, 4), (5, 4)})
        self.snotlings(w, [(7, 6), (7, 5), (7, 4)])
        answer = {**GEMS_15, "approach": {"method": "earn_gems", "x": 9, "y": 5}, "plan_b": {"method": "forage"}}
        ex, combat = Executive(fallback), engines.Combat()
        ctx = ctx_of(w)
        ex.adopt(publish.publication(answer, ctx), ctx, 0.0)
        ctx.approach = ex.approach
        self.assertTrue(combat.applies(ctx, Work(((9, 5),), "cut")))
        self.assertEqual(combat.now.kind, "hold")
        ex.step(ctx, 10.0)
        self.assertEqual(ex.plan, "B")
        self.assertTrue(ex.reports[-1].why.startswith("held up: snotling"))
        ctx.approach = ex.approach
        combat.applies(ctx, None)
        self.assertIs(combat.now.approach, ex.approach)

    def test_a_fight_from_safe_ground_steps_off_it_to_swing(self):
        w = world(OPEN, safe={(5, 5), (4, 5), (4, 4), (5, 4)})
        self.snotlings(w, [(6, 5)])
        action = engines.Combat().swing_at(ctx_of(w), w.entities[0])
        self.assertIsInstance(action, actions.Walk)
        self.assertNotIn(action.path[-1], w.maps[MAP].safe)

    def test_at_the_bar_she_drinks_a_potion_she_carries(self):
        w = world(OPEN)
        w.held_supplies = [InventorySupply(9, "small_potion")]
        self.snotlings(w, [(6, 5)])
        combat = engines.Combat()
        ctx = ctx_of(w, bar=5)
        combat.now = combat.make(ctx, "fight", w.entities[0], "snotling")
        w.health = 4
        self.assertIsInstance(combat.act(ctx_of(w, bar=5), None), actions.Drink)

    def test_at_the_bar_with_no_potion_she_breaks_off_and_says_so(self):
        w = world(OPEN, safe={(0, 0), (1, 0), (0, 1)})
        self.snotlings(w, [(6, 5)])
        combat = engines.Combat()
        ctx = ctx_of(w, bars={"snotling": 5})
        combat.now = combat.make(ctx, "fight", w.entities[0], "snotling")
        w.health = 4
        ctx = ctx_of(w, bars={"snotling": 5})
        combat.act(ctx, None)
        self.assertEqual(combat.now.kind, "back_off")
        self.assertTrue(any("broke off" in n for n in ctx.memory.take_news()))

    def test_a_close_call_is_rolled_and_a_bold_agent_leans_to_fight(self):
        fight = engines.Option("fight", "fight", harm.Course())
        back = engines.Option("back off", "back_off", harm.Course())
        scored = [(0.0, fight), (0.0, back)]
        bold = sum(engines.roll(scored, 10, random.Random(i))[0] is fight for i in range(400))
        timid = sum(engines.roll(scored, 0, random.Random(i))[0] is fight for i in range(400))
        self.assertGreater(bold, timid)
        self.assertIs(engines.roll([(5.0, fight), (0.0, back)], 0, random.Random(1))[0], fight)

    def test_the_odds_count_deaths(self):
        w = world(OPEN, health=1)
        self.snotlings(w, [(6, 5), (6, 6), (5, 6), (4, 6)])
        self.assertGreater(harm.odds(ctx_of(w).state, harm.Course()).death, 0.5)

    def test_a_wall_stops_a_chaser(self):
        rows = ["ggggggggggg"] * 5 + ["wwwwwwwwwww"] + ["ggggggggggg"] * 5
        w = world(rows, at=(5, 3))
        self.snotlings(w, [(5, 8)])
        self.assertEqual(harm.odds(ctx_of(w).state, harm.Course()).hits_taken, 0)


class TerritoryTest(unittest.TestCase):
    def test_a_snotling_that_keeps_to_grass_does_not_follow_onto_the_road(self):
        rows = ["ggggggdddddd"] * 12
        w = world(rows, at=(7, 5))
        w.entities = [Entity("npc", 101, (5, 5), "snotling")]
        w.hostile_types.add(("npc", "snotling"))
        s = ctx_of(w).state
        free = harm.odds(s, harm.Course(), territory={})
        kept = harm.odds(s, harm.Course(), territory={"snotling": ["grass"]})
        self.assertGreater(free.hits_taken, 0)
        self.assertEqual(kept.hits_taken, 0)


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


class ShelfTest(unittest.TestCase):
    def test_a_shelf_is_never_where_she_stands_to_read(self):
        w = world(OPEN, at=(2, 5))
        w.view.readable[(8, 5)] = True
        w.entities = [Entity("supply", 50 + i, p, "bronze_mail", gem_price=20) for i, p in enumerate([(7, 5), (7, 4), (7, 6)])]
        action = engines.Opportunities().pick(ctx_of(w))
        self.assertIsInstance(action, actions.Walk)
        self.assertNotIn(action.path[-1], {(7, 5), (7, 4), (7, 6)})


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
    def test_the_brief_carries_clues_entrances_its_chain_and_what_just_happened(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        mem.add_clue(Clue("seek the door by the old oak", "helper", "town_helper", MAP, 3, 3))
        mem.note("backed off from snotling; clear now near 4,4")
        ctx = ctx_of(world(OPEN), mem)
        text = describe(Briefing(ctx, "starting", "nothing", [], [], 0.0), {"chain": ["beat the world", "hold 15 gems"]})
        for part in ("seek the door by the old oak", "1:9,9", "hold 15 gems", "backed off from snotling"):
            self.assertIn(part, text)

    def test_a_reaction_note_is_news_once(self):
        mem = Memory()
        mem.note("engaged: fight snotling", news=False)
        mem.note("broke off")
        self.assertEqual(mem.take_news(), ["broke off"])
        self.assertEqual(mem.take_news(), [])
        self.assertEqual(len(mem.happened), 2)


class LeversTest(unittest.TestCase):
    def test_a_reckless_agent_weighs_death_lightly(self):
        self.assertLess(Levers(risk=10).death_weight, Levers(risk=5).death_weight)
        self.assertLess(Levers(risk=5).death_weight, Levers(risk=0).death_weight)


if __name__ == "__main__":
    unittest.main()
