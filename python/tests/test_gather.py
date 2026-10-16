"""Gather (A22, A88): one search for the place to work, under one ground filter and one price."""

import random
import unittest
from unittest import mock

from agentrealm_adk import supplies
from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS, Directives
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.memory import Memory
from agentrealm_agent.plan import Plan, parse_directives_goal
from agentrealm_agent.states import PlayContext, dispatch, gather_outcome
from agentrealm_agent.states import gather as gather_mod
from agentrealm_adk.hostile_ground import GATHER_HOSTILE_RADIUS, GATHER_SHADOW_MARGIN, danger, is_safe_ish
from agentrealm_agent.states.gather_safe import gather_ground, route_clear
from agentrealm_adk.world import STILL_TICKS, Entity, WorldModel, chebyshev
from agentrealm_adk.gem_yield import NO_EFFECT_ZONE_CUTS, REGION_SIZE, GemYieldTracker, fell_yield, record_cut, region_of
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.zone_discovery import apply_zone


def grid(rows: list[str], at=(1, 1)) -> WorldModel:
    """A world from ``rows``; a space is a cell not known yet."""
    glyph = {".": "dirt", "g": "grass", "b": "bush", "#": "wall", "l": "lava", "t": "tree"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != " ":
                w.view.tiles[(x, y)] = glyph.get(ch, "dirt")
    w.terrain_center, w.terrain_map = at, 7
    w.attack_range = 1
    w.gems = 0
    return w


def fence(w: WorldModel) -> WorldModel:
    """Wall in the known ground: no unexplored ground left to go for."""
    tiles = w.view.tiles
    for x, y in list(tiles):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (x + dx, y + dy) not in tiles:
                    tiles[(x + dx, y + dy)] = "wall"
    return w


def safe(w: WorldModel, *cells) -> None:
    for x, y in cells:
        apply_zone(w, 7, x, y, {"safe": True, "brightness": 1})


def ctx(w: WorldModel, goals: list[str], m: Memory | None = None, **policy_kw) -> PlayContext:
    """Directives ``goals`` become the plan stack (``gather_gems:N`` → a ``gather_gems`` op)."""
    kw = {"pickup": False, "on_hostile": "ignore", "goals": ["explore"], **policy_kw}
    plan = Plan.from_directives(directive_goals=goals, directive_params=dict(PARAM_DEFAULTS))
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", **kw),
        random.Random(0),
        directives=Directives(goals=goals),
        plan=plan,
    )


def outcome(w: WorldModel, m: Memory | None = None, **policy_kw):
    kw = {"on_hostile": "ignore", **policy_kw}
    return gather_outcome(w, m or Memory(), Policy(**kw))


POLICY = Policy(on_hostile="ignore")


class GatherShorthandTest(unittest.TestCase):
    def test_count_is_parsed(self):
        self.assertEqual(parse_directives_goal("gather_gems:20"), {"op": "gather_gems", "count": 20})

    def test_bad_counts_are_dropped(self):
        for text in ("gather_gems:x", "gather_gems:-3"):
            self.assertIsNone(parse_directives_goal(text), text)


class WorldGemsTest(unittest.TestCase):
    def test_snapshot_inventory_sets_gems(self):
        w = WorldModel(character_id=1)
        self.assertIsNone(w.gems)
        w.apply_observation({"complete": True, "version": 1, "snapshot": {"inventory": {"gems": 7}}})
        self.assertEqual(w.gems, 7)

    def test_inventory_without_gems_keeps_the_counter(self):
        w = WorldModel(character_id=1)
        w.gems = 3
        w.apply_observation({"complete": True, "version": 1, "snapshot": {"inventory": {"armed": None}}})
        self.assertEqual(w.gems, 3)




class GatherGroundTest(unittest.TestCase):
    def test_field_grass_with_no_hostile_near_qualifies(self):
        """No safe-zone read is needed: the open field is where the gems are."""
        w = grid(["ggg"], at=(1, 0))
        self.assertTrue(gather_ground(w, (1, 0), Policy()))

    def test_a_hostile_that_hit_us_bars_the_full_radius(self):
        w = grid(["g" * 12], at=(0, 0))
        w.entities = [Entity("npc", 1, (GATHER_HOSTILE_RADIUS, 0))]
        w.attacker, w.attacked_tick, w.tick = ("npc", 1), 100, 100
        self.assertFalse(gather_ground(w, (0, 0), Policy(hostile=["npc"])))
        w.entities = [Entity("npc", 1, (GATHER_HOSTILE_RADIUS + 1, 0))]
        self.assertTrue(gather_ground(w, (0, 0), Policy(hostile=["npc"])))

    def test_a_hostile_that_has_not_hit_us_bars_only_weapon_reach_plus_a_step(self):
        """A hostile that has not attacked does not bar the full danger radius."""
        w = grid(["g" * 12], at=(0, 0))
        w.attack_range = 2
        bar = w.attack_range + GATHER_SHADOW_MARGIN
        w.hostile_types.add(("npc", "gnawer"))
        pol = Policy(hostile=["npc"], hostile_range=1)
        w.entities = [Entity("npc", 1, (bar, 0), "gnawer")]
        self.assertFalse(gather_ground(w, (0, 0), pol))
        w.entities = [Entity("npc", 1, (bar + 1, 0), "gnawer")]
        self.assertTrue(gather_ground(w, (0, 0), pol))

    def test_the_bar_is_a_step_past_hostile_range_when_that_is_further(self):
        """A bar inside hostile_range would send Gather where Retreat starts."""
        w = grid(["g" * 12], at=(0, 0))
        w.hostile_types.add(("npc", "gnawer"))
        pol = Policy(hostile=["npc"], hostile_range=3)
        w.entities = [Entity("npc", 1, (4, 0), "gnawer")]
        self.assertFalse(gather_ground(w, (0, 0), pol))
        w.entities = [Entity("npc", 1, (5, 0), "gnawer")]
        self.assertTrue(gather_ground(w, (0, 0), pol))

    def test_gather_never_picks_a_cell_where_retreat_would_fire(self):
        """Hurt to the health floor, no cell Gather accepts has Retreat start
        there, nor after the hostile steps once toward it: the two never pace."""
        from agentrealm_agent.states.retreat import RetreatState

        w = grid(["g" * 14] * 3, at=(0, 1))
        safe(w, (0, 1))
        w.health, w.max_health, w.lives = 1, 10, 3
        w.hostile_types.add(("npc", "wartlurch"))
        hostile = Entity("npc", 1, (9, 1), "wartlurch")
        w.entities = [hostile]
        for hostile_range in (1, 2, 3, 4):
            c = ctx(w, ["gather_gems:3"], on_hostile="flee", hostile=["npc"], hostile_range=hostile_range)
            w.pos = (hostile.pos[0] - hostile_range, 1)
            self.assertTrue(RetreatState().guard(w, c), "Retreat fires within hostile_range")
            picked = [p for p in w.view.tiles if gather_ground(w, p, c.policy)]
            self.assertTrue(picked, hostile_range)
            for cell in picked:
                for step in (0, 1):
                    toward = (hostile.pos[0] - step if cell[0] < hostile.pos[0] else hostile.pos[0] + step, 1)
                    w.pos, w.entities = cell, [Entity("npc", 1, toward, "wartlurch")]
                    self.assertFalse(RetreatState().guard(w, c), (hostile_range, cell, toward))
                    w.entities = [hostile]

    def test_safe_zone_cells_qualify(self):
        """Breaking a block works in a safe zone: Gather does not ban safe ground."""
        w = grid(["ggg"], at=(1, 0))
        safe(w, (0, 0))
        self.assertTrue(gather_ground(w, (0, 0), Policy()))

    def test_hazard_and_unknown_cells_disqualify(self):
        w = grid(["gl"], at=(0, 0))
        self.assertFalse(gather_ground(w, (1, 0), Policy(avoid_blocks=["lava"])))
        self.assertFalse(gather_ground(w, (5, 5), Policy()))


class SafeIshTest(unittest.TestCase):
    """The safe-zone blocks a hurt character walks first: judged on the exact
    block, where nothing hurts (Manual §11 Combat, A162)."""

    def test_known_safe_tile_qualifies(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        self.assertTrue(is_safe_ish(w, (1, 0)))

    def test_a_hostile_beside_it_does_not_disqualify(self):
        """A hostile may walk in but deals nothing there."""
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        w.entities = [Entity("npc", 1, (2, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        self.assertTrue(is_safe_ish(w, (1, 0)))

    def test_a_hazard_on_it_does_not_disqualify(self):
        w = grid(["glg"], at=(0, 0))
        safe(w, (1, 0))
        self.assertTrue(is_safe_ish(w, (1, 0)), "damaging blocks do not hurt on a safe block")

    def test_a_block_beside_a_safe_zone_or_a_respawn_anchor_does_not_qualify(self):
        w = grid(["ggg"], at=(2, 0))
        safe(w, (0, 0))
        w.record_respawn_anchor(7, (0, 0))
        self.assertFalse(is_safe_ish(w, (1, 0)))
        self.assertFalse(is_safe_ish(w, (2, 0)))



class WorkInReachTest(unittest.TestCase):
    """What is in reach costs no walk: it is worked unless the walk Gather
    would take costs less in all (``YieldPricingTest`` has that case)."""

    def test_a_bush_is_never_cut(self):
        """Bushes drop berries, not gems."""
        w = grid([".b."], at=(0, 0))
        out = outcome(w)
        self.assertFalse(any(i.get("verb") == "Use" for i in out.intents or []))

    def test_unlisted_supply_codes_are_not_treated_as_gem_piles(self):
        w = fence(grid(["...", "..."], at=(1, 0)))
        w.entities = [Entity("supply", 9, (2, 0), "gem_cache_5")]
        self.assertIsNone(outcome(w, pickup=False).intents)

    def test_priced_gem_supply_is_shop_stock_not_a_pile(self):
        w = fence(grid(["...", "..."], at=(1, 0)))
        w.entities = [Entity("supply", 9, (2, 0), "gem", gem_price=3)]
        self.assertIsNone(outcome(w, pickup=False).intents)

    def test_takes_a_gem_pile_beside_it(self):
        w = grid(["g.g"], at=(1, 0))
        safe(w, (1, 0), (2, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem")]
        self.assertEqual(outcome(w, pickup=False).intents[0]["verb"], "Take")

    def test_cuts_the_grass_it_stands_on(self):
        w = grid(["ggg"], at=(1, 0))
        self.assertEqual(outcome(w).reason, "cut grass")

    def test_safe_zone_grass_is_cut_like_any_other(self):
        """Whether a safe zone's grass cuts is learned from its cuts, not assumed (``NoEffectGroundTest``)."""
        w = grid(["ggg"], at=(1, 0))
        safe(w, (0, 0), (1, 0), (2, 0))
        self.assertEqual(outcome(w).reason, "cut grass")

    def test_skips_grass_with_a_hostile_near(self):
        w = grid(["ggg"], at=(1, 0))
        w.entities = [Entity("npc", 4, (2, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        self.assertIsNone(outcome(w, hostile=["npc"]).intents)

    def test_what_is_in_reach_comes_before_the_committed_walk_and_keeps_it(self):
        w = grid(["ggg....g"], at=(1, 0))
        m = Memory(path=[(2, 0)], goal="gather", gather_target=("grass", (7, 0)))
        self.assertEqual(outcome(w, m).reason, "cut grass")
        self.assertEqual(m.gather_target, ("grass", (7, 0)), "the walk is not given up for the cut")
        w.view.tiles[(1, 0)] = "dirt"
        w.entities = [Entity("supply", 9, (0, 0), "gem")]
        self.assertEqual(outcome(w, m, pickup=False).intents[0]["verb"], "Take")


class GatherArmsACutterTest(unittest.TestCase):
    """A cut goes out with a tool that cuts: a cut with a potion armed does
    nothing, and its no-effect would mark good ground uncuttable."""

    def test_arms_a_cutting_tool_with_the_cut(self):
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(5, "pocket_knife")]
        out = outcome(w)
        self.assertTrue(out.paced, "the Use goes out with the Arm")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 5}, {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 0}}])
        self.assertEqual(out.reason, "arm pocket_knife, cut grass")

    def test_a_tool_that_cuts_is_kept(self):
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "bronze_sword"
        w.held_supplies = [InventorySupply(5, "pocket_knife")]
        out = outcome(w)
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "block", "x": 1, "y": 0}}])
        self.assertFalse(out.paced)

    def test_the_weapon_swapped_out_comes_back_once_gathering_is_over(self):
        """A weapon swapped out for a cutter is armed again once the gather work ends."""
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "bronze_mallet"
        w.held_supplies = [InventorySupply(5, "pocket_knife")]
        m = Memory()
        out = dispatch(w, ctx(w, ["gather_gems:5"], m))
        self.assertEqual(out.state, "Gather")
        self.assertEqual([i["verb"] for i in out.intents], ["Arm", "Use"])
        self.assertEqual(m.gather_rearm, ("bronze_mallet", "pocket_knife"))
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(6, "bronze_mallet")]
        out = dispatch(w, ctx(w, [], m))
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 6}])
        self.assertIsNone(m.gather_rearm)

    def test_an_arm_made_since_is_not_undone(self):
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "bronze_sword"  # Equip's upgrade, after the cut
        w.held_supplies = [InventorySupply(6, "bronze_mallet"), InventorySupply(5, "pocket_knife")]
        m = Memory(gather_rearm=("bronze_mallet", "pocket_knife"))
        self.assertNotEqual(dispatch(w, ctx(w, [], m)).state, "Gather")
        self.assertIsNone(m.gather_rearm)

    def test_a_potion_swapped_out_is_not_put_back(self):
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(5, "pocket_knife")]
        m = Memory()
        outcome(w, m)
        self.assertIsNone(m.gather_rearm)

    def test_the_cutter_is_picked_by_the_one_weapon_rule(self):
        """A99: Gather arms the cutter Equip's rule ranks best (within the
        hunting ceiling, then damage), not the first that cuts."""
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 5}, {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 0}}])
        self.assertIsNone(m.gather_rearm, "the knife cuts: it was only bettered, not swapped out")

    def test_a_cutter_over_the_hunting_ceiling_is_not_armed_to_cut(self):
        w = grid(["ggg"], at=(1, 0))
        apply_zone(w, w.map_id, 40, 40, {"safe": False, "brightness": 1, "strength_ceiling": 7})
        w.armed_code = "bronze_sword"  # 2 + 4 = 6
        w.held_supplies = [InventorySupply(5, "iron_sword")]  # 2 + 8 = 10
        self.assertEqual(outcome(w).intents, [{"verb": "Use", "target": {"kind": "block", "x": 1, "y": 0}}])
        w.armed_code = "bronze_mallet"  # cannot cut: the cutter within the ceiling is armed
        w.held_supplies = [InventorySupply(5, "iron_sword"), InventorySupply(6, "bronze_sword")]
        self.assertEqual(outcome(w).intents[0], {"verb": "Arm", "supply_id": 6})

    def test_with_nothing_that_cuts_it_cuts_with_what_is_in_hand(self):
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "fake_cleaver"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        self.assertEqual(outcome(w).intents, [{"verb": "Use", "target": {"kind": "block", "x": 1, "y": 0}}])



class GatherFellsTreesTest(unittest.TestCase):
    """A161: with an item that chops held, a wild tree is a Gather target
    under the one price, its yield at least grass's on that ground; without
    one it is not (Manual §16 How to make money)."""

    def test_a_tree_is_a_target_with_a_sword_held(self):
        w = fence(grid(["....t"], at=(0, 0)))
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(m.gather_target, ("tree", (4, 0)))
        self.assertEqual(m.path[-1], (3, 0), "the walk ends beside the tree")
        self.assertEqual(out.intents[0], {"verb": "SetPosition", "x": 1, "y": 0})
        self.assertEqual(m.gather_status, "walking to a tree")

    def test_a_tree_is_no_target_with_only_a_knife(self):
        w = fence(grid(["....t"], at=(0, 0)))
        w.armed_code = "pocket_knife"
        m = Memory()
        out = outcome(w, m)
        self.assertIsNone(m.gather_target)
        self.assertIsNone(out.intents)

    def test_a_tree_beside_it_is_felled_with_the_sword_armed(self):
        w = fence(grid(["..t"], at=(1, 0)))
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 5}, {"verb": "Use", "target": {"kind": "block", "x": 2, "y": 0}}])
        self.assertEqual(out.reason, "arm bronze_sword, fell a tree")
        self.assertEqual(m.gather_status, "felling a tree")
        self.assertIsNone(m.gather_rearm, "the knife cuts: it was only bettered")

    def test_the_walk_ends_within_the_choppers_reach(self):
        """A116: the stand cells come from the chopper's reach, not a constant."""
        w = fence(grid(["......t"], at=(0, 0)))
        w.armed_code = "bronze_sword"
        k = KnowledgeBase.empty("fake-world")
        k.items["bronze_sword"] = {"attack_range": 2}
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"), knowledge=k, op={"op": "gather_gems", "count": 3})
        self.assertEqual(m.gather_target, ("tree", (6, 0)))
        self.assertEqual(m.path[-1], (4, 0), "two cells off, the sword's learned reach")
        w.pos = (4, 0)
        out = gather_outcome(w, m, Policy(on_hostile="ignore"), knowledge=k, op={"op": "gather_gems", "count": 3})
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "block", "x": 6, "y": 0}}])

    def test_blocked_by_hostile_when_only_a_hostile_bars_the_tree(self):
        w = grid(["...t"], at=(0, 0))
        w.armed_code = "bronze_sword"
        w.entities = [Entity("npc", 9, (3, 1), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))
        m = Memory()
        out = outcome(w, m, hostile=["npc"])
        self.assertIsNone(out.intents)
        self.assertEqual(m.gather_status, gather_mod.BLOCKED)
        w.armed_code = "pocket_knife"  # no chopper: the tree is no target, so nothing is barred
        out = outcome(w, Memory(), hostile=["npc"])
        self.assertIsNone(out.intents)

    def test_trees_felled_on_this_map_raise_the_trees_yield(self):
        k = KnowledgeBase.empty("fake-world")
        for i in range(40):
            record_cut(k, 7, (30, 1 + i % 10), "tree", i, i % 2 == 0)
        _, by_region = gather_mod.region_yields(k, 7)
        odds = gather_mod.GemOdds(4, 0.2, by_region, fells=gather_mod.fell_totals(k, 7))
        self.assertLess(odds.work("tree", (1, 1)), odds.work("grass", (1, 1)))

    def test_a_trees_yield_is_at_least_grass_on_that_ground(self):
        odds = gather_mod.GemOdds(4, 0.2, {(0, 0): 0.4})
        for at in ((1, 1), (40, 40)):
            self.assertLessEqual(odds.work("tree", at), odds.work("grass", at), at)
        for rate in (0.0, 0.2, 0.5):
            self.assertGreaterEqual(fell_yield(rate), rate)

    def test_the_price_picks_the_cheaper_of_grass_and_tree_by_walk_and_yield(self):
        """From (14, 0), grass at (8, 0) is 6 steps west in region 0,0; a tree
        at (22, 0) is 7 east in region 1,0 (worked from (21, 0)). At one yield
        the shorter walk wins; with region 1,0 yielding more, the tree does,
        and with the tree nearer it wins at one yield."""

        def pick(tree=(22, 0), knowledge=None):
            w = fence(grid(["." * 30], at=(14, 0)))
            w.tick = 10_000  # the recorded cuts below have grown back
            w.view.tiles[(8, 0)], w.view.tiles[tree] = "grass", "tree"
            w.armed_code = "bronze_sword"
            m = Memory()
            gather_outcome(w, m, Policy(on_hostile="ignore"), knowledge=knowledge, op={"op": "gather_gems", "count": 3})
            return m.gather_target

        self.assertEqual(pick(), ("grass", (8, 0)), "same yield: the shorter walk")
        self.assertEqual(pick(tree=(19, 0)), ("tree", (19, 0)), "same yield, the tree nearer")
        k = KnowledgeBase.empty("fake-world")
        for i in range(40):
            record_cut(k, 7, (2, 1 + i % 10), "grass", i, i % 20 == 0)  # region 0,0: poor, 2 in 40
            record_cut(k, 7, (20, 1 + i % 10), "grass", 40 + i, i % 2 == 0)  # region 1,0: 1 in 2
        self.assertEqual(pick(knowledge=k), ("tree", (22, 0)), "the tree's yield pays for its longer walk")


class TargetSearchTest(unittest.TestCase):
    """Nothing in reach: Gather walks to the cheapest place to work, priced by
    its walk plus the work there (``GemOdds``)."""

    def test_walks_to_field_grass(self):
        w = grid(["..g"], at=(0, 0))
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents[0], {"verb": "SetPosition", "x": 1, "y": 0})
        self.assertEqual((m.goal, m.gather_target), ("gather", ("grass", (2, 0))))

    def test_walks_past_a_nearer_bush_to_grass(self):
        w = grid(["g..b"], at=(2, 0))
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (0, 0)))

    def test_cuts_its_way_along_the_grass_and_never_to_a_bush(self):
        w = fence(grid(["b..........", "...gggggggg"], at=(4, 0)))
        m = Memory()
        for _ in range(40):
            out = outcome(w, m)
            verb = out.intents[0]["verb"] if out.intents else None
            if verb == "SetPosition":
                w.pos = (out.intents[0]["x"], out.intents[0]["y"])
                self.assertEqual(m.gather_target[0], "grass")
            elif verb == "Use":
                t = out.intents[0]["target"]
                w.view.tiles[(t["x"], t["y"])] = "dirt"  # cut: bare until it grows back
            else:
                break
        self.assertNotIn("grass", w.view.tiles.values())
        self.assertEqual(w.view.tiles[(0, 0)], "bush", "the bush is left standing")

    def test_walks_to_a_known_pile_first(self):
        w = grid(["g...."], at=(1, 0))
        safe(w, (0, 0), (4, 0))
        w.entities = [Entity("supply", 9, (4, 0), "gem")]
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("pile", (4, 0)))
        self.assertEqual(m.path[-1], (4, 0))

    def test_a_pile_is_worth_the_cuts_its_sure_gem_saves(self):
        """With 5 gems to go at 1 in 5, grass costs 25 cuts and a pile 1 take
        and 20 cuts: a pile up to 3 steps further than the grass wins, 5 do not."""
        op = {"op": "gather_gems", "count": 5}
        for pile_x, kind in ((9, "pile"), (11, "grass")):
            w = fence(grid(["." * 20], at=(0, 0)))
            w.view.tiles[(6, 0)] = "grass"
            w.entities = [Entity("supply", 9, (pile_x, 0), "gem")]
            m = Memory()
            gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
            self.assertEqual(m.gather_target[0], kind, pile_x)

    def test_pile_target_path_is_kept_between_ticks(self):
        w = grid(["......"], at=(0, 0))
        safe(w, (5, 0))
        w.entities = [Entity("supply", 9, (5, 0), "gem")]
        m = Memory()
        outcome(w, m)
        path = list(m.path)
        with mock.patch.object(gather_mod, "_replan") as replan:
            outcome(w, m)
        replan.assert_not_called()
        self.assertEqual(m.path, path)

    def test_target_that_stops_qualifying_is_dropped(self):
        w = fence(grid(["...g"], at=(0, 0)))
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (3, 0)))
        w.view.tiles[(3, 0)] = "dirt"  # cut by someone else
        out = outcome(w, m)
        self.assertIsNone(out.intents)
        self.assertEqual((m.path, m.goal, m.gather_target), ([], "", None))

    def test_no_target_keeps_another_states_path(self):
        w = fence(grid(["...."], at=(0, 0)))
        m = Memory(path=[(1, 0), (2, 0)], goal="explore")
        self.assertIsNone(outcome(w, m).intents)
        self.assertEqual((m.path, m.goal), ([(1, 0), (2, 0)], "explore"))

    def test_the_walk_goes_round_a_known_hostiles_reach(self):
        """No walk crosses a known hostile's reach: the search treats it as a
        wall and finds the way round, rather than giving the target up."""
        w = fence(grid(["." * 20] * 9, at=(2, 4)))
        w.view.tiles[(16, 4)] = "grass"
        w.entities = [Entity("npc", 9, (9, 4), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))
        m = Memory()
        outcome(w, m, hostile=["npc"], hostile_range=2)
        self.assertEqual(m.gather_target, ("grass", (16, 4)))
        self.assertFalse([p for p in m.path if chebyshev(p, (9, 4)) <= 2], m.path)


class NoEffectGroundTest(unittest.TestCase):
    """Ground whose cuts have no effect (town grass, say) is learned from the
    cuts, not assumed (GAME_NOTES.md), and then left alone."""

    def test_a_cut_with_no_effect_holds_out_its_cell_and_a_few_mark_the_safe_zone(self):
        w = grid(["ggggg..."], at=(0, 0))
        safe(w, *[(x, 0) for x in range(5)])
        tracker = GemYieldTracker()
        for x in range(NO_EFFECT_ZONE_CUTS - 1):
            tracker.note_no_effect(w, (x, 0), "grass", w.tick)
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_target, ("grass", (NO_EFFECT_ZONE_CUTS - 1, 0)), "below the bar: the rest stay cuttable")
        self.assertEqual(m.gather_status, gather_mod.NO_EFFECT)
        tracker.note_no_effect(w, (NO_EFFECT_ZONE_CUTS - 1, 0), "grass", w.tick)
        m = Memory()
        out = gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (5, 0)), "the whole zone is uncuttable, its edges too")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")


class NothingToGoForTest(unittest.TestCase):
    def test_walks_to_known_grass_far_away_when_nothing_is_nearer(self):
        w = grid(["#" * 33, "#" + "." * 30 + "g#", "#" * 33], at=(1, 1))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(c.memory.gather_target, ("grass", (31, 1)))
        self.assertEqual(c.memory.gather_status, "walking to grass", "a walk is not a cut")

    def test_with_no_grass_known_it_walks_to_unexplored_ground_itself(self):
        w = grid([".....", "....."], at=(0, 0))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(c.memory.gather_target[0], gather_mod.FRONTIER)
        self.assertEqual(c.memory.gather_status, "walking to unexplored ground")

    def test_with_nowhere_to_go_it_asks_the_planner(self):
        # Its search already went for unexplored ground: the safe default never explores for it (A120).
        w = fence(grid([".....", "....."], at=(0, 0)))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Gather", None, True), out.reason)
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]
        self.assertEqual([(s["why"], s["op"]["op"]) for s in asks], [("no gather target: no cuttable cell in view", "gather_gems")])
        self.assertEqual(c.memory.gather_status, gather_mod.NONE_CUTTABLE)

    def test_standing_in_a_barren_region_says_so(self):
        w = grid(["ggg"], at=(1, 0))
        c = ctx(w, ["gather_gems:3"])
        with mock.patch.object(gather_mod, "barren_regions", return_value={(0, 0)}):
            dispatch(w, c)
        self.assertEqual(c.memory.gather_status, gather_mod.REGION_BARREN)


class HostileNearTest(unittest.TestCase):
    """A hostile that stays near without attacking only bars the cells near
    it; whether to work near it, or fight it, is not Gather's call."""

    def near(self, off: int, on_hostile: str = "flee", at=(10, 0)) -> tuple[WorldModel, PlayContext]:
        w = grid(["g" * 40], at=at)
        w.entities = [Entity("npc", 9, (at[0] + off, 0), "wartlurch")]
        w.hostile_types.add(("npc", "wartlurch"))  # a type seen attacking (survival.is_hostile)
        return w, ctx(w, ["gather_gems:3"], on_hostile=on_hostile, hostile=["npc"], hostile_range=2)

    def test_cuts_beside_a_hostile_that_has_not_attacked(self):
        w, c = self.near(4)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.intents[0]["verb"], "Use", "4 blocks off and harmless: keep cutting")
        self.assertEqual(c.memory.gather_status, gather_mod.CUTTING)

    def test_staying_near_for_long_changes_nothing(self):
        w, c = self.near(4, on_hostile="fight")
        for _ in range(60):
            w.tick += c.plan.tick_hz
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents[0]["verb"]), ("Gather", "Use"))
            self.assertEqual(out.intents[0]["target"]["kind"], "block", "Gather never swings at it")

    def test_one_nearer_bars_only_the_cells_near_it(self):
        w, c = self.near(2)
        m = Memory()
        out = gather_outcome(w, m, c.policy, op={"op": "gather_gems", "count": 3})
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(m.gather_target[0], "grass")
        self.assertTrue(gather_ground(w, m.gather_target[1], c.policy))
        self.assertGreater(chebyshev(m.gather_target[1], (12, 0)), c.policy.hostile_range + 1)

    def test_an_npc_of_a_type_never_seen_attacking_bars_nothing(self):
        w, c = self.near(1, on_hostile="fight")
        w.hostile_types.clear()  # townsfolk standing near (survival.is_hostile)
        self.assertEqual(dispatch(w, c).intents[0]["verb"], "Use")


class GatherReflexTest(unittest.TestCase):
    def test_flee_outranks_gather(self):
        w = grid(["ggggg"], at=(2, 0))
        safe(w, (0, 0))
        w.entities = [Entity("npc", 4, (3, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        m = Memory(path=[(1, 0)], goal="gather", gather_target=("grass", (0, 0)))
        out = dispatch(w, ctx(w, ["gather_gems:3"], m, on_hostile="flee", hostile=["npc"], hostile_range=2))
        self.assertEqual(out.state, "Flee")
        self.assertEqual(m.path, [])

    def test_escape_outranks_gather(self):
        w = grid([".lg"], at=(1, 0))
        safe(w, (2, 0))
        out = dispatch(w, ctx(w, ["gather_gems:3"], avoid_blocks=["lava"]))
        self.assertEqual(out.state, "Escape")

    def test_fight_outranks_gather(self):
        w = grid(["ggg"], at=(1, 0))
        w.entities = [Entity("character", 5, (2, 0), code="peer")]
        w.health, w.lives = 500, 10
        w.threat.record(("character", "peer"), 1)
        from agentrealm_adk.directives import PARAM_DEFAULTS

        c = ctx(
            w,
            ["gather_gems:3"],
            Memory(path=[(0, 0)], goal="gather"),
            on_hostile="fight",
            hostile=["character"],
            hostile_range=2,
        )
        c.params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}
        out = dispatch(w, c)
        self.assertEqual(out.state, "Fight")
        self.assertEqual(out.intents[0]["target"], {"kind": "character", "character_id": 5})



class GatherStatusStaleTest(unittest.TestCase):
    """``gather_status`` speaks only for a decision Gather made."""

    def test_cleared_when_a_reflex_preempts_gather(self):
        from agentrealm_agent.brain import decide

        w = grid(["ggggg"], at=(2, 0))
        m = Memory(gather_status=gather_mod.CUTTING)
        w.entities = [Entity("npc", 4, (3, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        c = ctx(w, ["gather_gems:3"], m, on_hostile="flee", hostile=["npc"], hostile_range=2)
        d = decide(w, m, c.policy, c.rng, directives=c.directives, plan=c.plan)
        self.assertIn("flee", d.reason.lower())
        self.assertEqual(m.gather_status, "")

    def test_kept_when_gather_decides(self):
        from agentrealm_agent.brain import decide

        w = grid(["ggg"], at=(1, 0))
        m = Memory()
        c = ctx(w, ["gather_gems:3"], m)
        decide(w, m, c.policy, c.rng, directives=c.directives, plan=c.plan)
        self.assertEqual(m.gather_status, gather_mod.CUTTING)


class GatherDispatchTest(unittest.TestCase):
    def test_gather_beats_explore_when_goal_active(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Gather")

    def test_gather_op_done_when_gem_count_met(self):
        w = grid(["ggg"], at=(1, 0))
        w.gems = 5
        safe(w, (1, 0))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Explore")
        self.assertIsNone(c.plan.current())

    def test_unknown_gem_count_is_not_done(self):
        w = grid(["ggg"], at=(1, 0))
        w.gems = None
        safe(w, (1, 0))
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Gather")

    def test_no_gather_op_no_gather(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        out = dispatch(w, ctx(w, []))
        self.assertEqual(out.state, "Explore")

    def test_no_target_in_sight_gather_moves_itself(self):
        w = grid(["....", "....", "...."], at=(1, 1))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertIsNotNone(out.intents, "Gather moves instead of the character standing still")
        self.assertEqual(c.memory.state, "Gather")

    def test_gather_cuts_once_a_target_appears(self):
        w = grid(["....", "....", "...."], at=(1, 1))
        c = ctx(w, ["gather_gems:3"])
        dispatch(w, c)
        w.pos = (1, 1)
        w.view.tiles[(1, 1)] = "grass"
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.reason, "cut grass")

    def test_unreachable_grass_leaves_unexplored_ground_to_go_for(self):
        w = grid(["....###", "....#g#", "....###"], at=(1, 1))
        safe(w, (5, 1))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(c.memory.gather_target[0], gather_mod.FRONTIER)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")


class GatherStatusTest(unittest.TestCase):
    """gather_status tells a walk from a cut, and a stall from progress."""

    def test_blocked_by_hostile_when_only_a_hostile_bars_the_grass(self):
        w = grid(["..g"], at=(0, 0))
        w.entities = [Entity("npc", 9, (2, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))
        m = Memory()
        out = outcome(w, m, hostile=["npc"])
        self.assertIsNone(out.intents)
        self.assertEqual(m.gather_status, gather_mod.BLOCKED)

    def test_flags_no_cut_for_a_while(self):
        w = grid(["#" * 33, "#" + "." * 30 + "g#", "#" * 33], at=(1, 1))
        m = Memory()
        tracker = GemYieldTracker()
        gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_status, "walking to grass")
        for _ in range(gather_mod.STALL_SECONDS):
            w.tick += 10
            gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_status, f"walking to grass, no cut for {gather_mod.STALL_SECONDS} s")
        tracker.last_cut_tick = w.tick
        gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_status, "walking to grass", "a cut restarts the clock")



class GatherPricesUnexploredGroundTest(unittest.TestCase):
    """Known grass is weighed against the edge of unexplored ground, each by
    its walk and the cuts the gems still needed take at the yield there."""

    # Walled in but for an opening at (0, 1): unexplored ground two steps
    # from (3, 1), known grass 37 steps off at (40, 1).
    ROWS = ["#" * 42, " " + "." * 39 + "g#", "#" * 42]

    def world(self) -> WorldModel:
        w = grid(self.ROWS, at=(3, 1))
        w.tick = 10_000  # the recorded cuts below have grown back
        return w

    def rich_far_region(self) -> KnowledgeBase:
        """The far grass's region gave 1 gem in 2 cuts over 40 cuts."""
        k = KnowledgeBase.empty("fake-world")
        for i in range(40):
            record_cut(k, 7, (40, 2 + i % 10), "grass", i, i % 2 == 0)
        return k

    def test_explores_near_rather_than_walk_to_far_grass_of_the_same_yield(self):
        w, m = self.world(), Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"), op={"op": "gather_gems", "count": 3})
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (1, 1)))
        self.assertEqual(m.gather_status, "walking to unexplored ground")

    def test_far_grass_wins_when_its_yield_outweighs_the_walk(self):
        k = self.rich_far_region()
        few, many = {"op": "gather_gems", "count": 3}, {"op": "gather_gems", "count": 100}
        m = Memory()
        gather_outcome(self.world(), m, Policy(on_hostile="ignore"), knowledge=k, op=few)
        self.assertEqual(m.gather_target[0], gather_mod.FRONTIER, "a few gems do not pay for the walk")
        m = Memory()
        gather_outcome(self.world(), m, Policy(on_hostile="ignore"), knowledge=k, op=many)
        self.assertEqual(m.gather_target, ("grass", (40, 1)), "many gems at a better yield do")

    def test_grass_as_near_as_the_edge_is_cut_first(self):
        w = self.world()
        w.view.tiles[(4, 1)] = "grass"
        m = Memory()
        out = gather_outcome(w, m, Policy(on_hostile="ignore"), op={"op": "gather_gems", "count": 3})
        self.assertEqual(m.gather_target, ("grass", (4, 1)))
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_a_named_region_is_worked_without_weighing_the_edge(self):
        w, m = self.world(), Memory()
        op = {"op": "gather_gems", "count": 3, "x": 40, "y": 1}
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, ("grass", (40, 1)))

    def test_the_edge_is_priced_again_once_the_walk_reveals_it(self):
        w, m = self.world(), Memory()
        op = {"op": "gather_gems", "count": 3}
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (1, 1)))
        w.view.tiles[(0, 1)] = "grass"  # revealed: grass beyond, the corridor closed off
        for p in ((-1, 0), (-1, 1), (-1, 2), (0, 0), (0, 2)):
            w.view.tiles[p] = "wall"
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, ("grass", (0, 1)))

    def test_an_edge_explore_gave_up_on_loses_to_known_grass(self):
        w, m = self.world(), Memory()
        nav_stuck.remember_give_up(m.nav_stuck, nav_stuck.goal_key("explore", 7, (1, 1)), "explore", w.tick)
        gather_outcome(w, m, Policy(on_hostile="ignore"), op={"op": "gather_gems", "count": 3})
        self.assertEqual(m.gather_target, ("grass", (40, 1)))

    def test_an_edge_with_no_ground_past_it_stops_being_one_at_the_read_there(self):
        """Reaching the edge, the terrain read shows what lies past it: no
        ground past the map's edge, and a wall in the opening. The edge is
        gone and the known grass is weighed alone."""
        w, m = self.world(), Memory()
        op = {"op": "gather_gems", "count": 3}
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (1, 1)))
        w.pos = (1, 1)
        rows = ["?" * 11] * 11
        rows[5] = "?" * 4 + "#" + "?" * 6  # (0, 1): the opening, a wall
        w.apply_terrain({"map_id": 7, "x0": -4, "y0": -4, "width": 11, "height": 11, "rows": rows,
                         "legend": {"#": {"block_type": "wall"}}, "map_width": 42, "map_height": 3})
        self.assertNotIn((1, 1), w.view.frontier(fords=False))
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, ("grass", (40, 1)))

    def test_a_walk_to_an_edge_ends_once_a_target_region_is_worked(self):
        w, m = self.world(), Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"), op={"op": "gather_gems", "count": 3})
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (1, 1)))
        named = {"op": "gather_gems", "count": 3, "x": 40, "y": 1}
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=named)
        self.assertEqual(m.gather_target, ("grass", (40, 1)))

    def test_a_walk_to_an_edge_ends_once_its_region_is_skipped(self):
        w, m = self.world(), Memory()
        op = {"op": "gather_gems", "count": 3}
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (1, 1)))
        with mock.patch.object(gather_mod, "barren_regions", return_value={(0, 0)}):
            gather_outcome(w, m, Policy(on_hostile="ignore"), op=op)
        self.assertEqual(m.gather_target, ("grass", (40, 1)))

    def test_safe_ground_edges_are_priced_like_any_other(self):
        w = self.world()
        safe(w, (0, 1), (1, 1))
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"), op={"op": "gather_gems", "count": 3})
        self.assertEqual(m.gather_target, (gather_mod.FRONTIER, (1, 1)))


def three_regions(at=(40, 8)) -> WorldModel:
    """Grass over three regions in a row (x 0–15, 16–31 and 32–47), walled in."""
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    w.alive, w.tick = True, 10_000
    for x in range(3 * REGION_SIZE):
        for y in range(REGION_SIZE):
            w.view.tiles[(x, y)] = "grass"
    w.terrain_center, w.terrain_map = at, 7
    return fence(w)


def sample(kb: KnowledgeBase, cell, cuts: int, gems: int) -> None:
    """``cuts`` cuts of ``cell`` long ago, ``gems`` of them with a gem."""
    for i in range(cuts):
        record_cut(kb, 7, cell, "grass", -10_000 - i, i < gems)


class YieldPricingTest(unittest.TestCase):
    """A region's grass is priced at its measured yield: poorer ground costs
    more cuts per gem, so better ground is worth a walk when enough gems are
    still needed."""

    def test_the_better_yield_pays_for_the_walk_only_when_enough_gems_are_needed(self):
        kb = KnowledgeBase.empty("fake-world")
        sample(kb, (40, 8), 20, 1)  # where Gather stands: 1 gem in 20
        sample(kb, (24, 8), 8, 2)  # the next region over: 1 gem in 4
        out = gather_outcome(three_regions(), Memory(), POLICY, knowledge=kb, op={"op": "gather_gems", "count": 1})
        self.assertEqual(out.reason, "cut grass", "one gem: not worth 9 steps")
        m = Memory()
        gather_outcome(three_regions(), m, POLICY, knowledge=kb, op={"op": "gather_gems", "count": 30})
        self.assertEqual((m.gather_target[0], region_of(m.gather_target[1])), ("grass", (1, 0)))
        self.assertEqual(m.gather_status, "walking to grass")

    def test_poor_ground_is_left_for_ground_not_sampled_yet(self):
        """Unsampled ground is worth the map's measured rate."""
        kb = KnowledgeBase.empty("fake-world")
        sample(kb, (40, 8), 54, 1)
        m = Memory()
        gather_outcome(three_regions(), m, POLICY, knowledge=kb, op={"op": "gather_gems", "count": 30})
        self.assertEqual(region_of(m.gather_target[1]), (1, 0))

    def test_in_the_better_region_it_does_not_drift_back(self):
        kb = KnowledgeBase.empty("fake-world")
        sample(kb, (40, 8), 54, 1)
        sample(kb, (24, 8), 8, 2)
        w = three_regions(at=(31, 8))
        w.view.tiles[(31, 8)] = "dirt"  # cut: the nearest grass left is in the poor region
        for y in range(REGION_SIZE):
            for x in range(26, 31):
                w.view.tiles[(x, y)] = "dirt"
        m = Memory()
        gather_outcome(w, m, POLICY, knowledge=kb, op={"op": "gather_gems", "count": 30})
        self.assertEqual(region_of(m.gather_target[1]), (1, 0), "5 steps on in its own region beat 1 into the poor one")



class TargetRegionTest(unittest.TestCase):
    """A ``gather_gems`` x, y is the planner's choice of region: Gather goes
    only for places there while it knows one there."""

    def test_the_op_x_y_names_the_region_gather_works_in(self):
        m = Memory()
        out = gather_outcome(three_regions(at=(24, 8)), m, POLICY, op={"op": "gather_gems", "count": 30, "x": 3, "y": 3})
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "the grass underfoot is not in the region named")
        self.assertEqual(region_of(m.gather_target[1]), (0, 0))
        self.assertEqual(m.gather_status, "walking to grass (region 0,0)")

    def test_an_unseen_named_region_is_walked_toward(self):
        w = three_regions()
        for y in range(REGION_SIZE):
            del w.view.tiles[(48, y)]  # an opening east
        m = Memory()
        out = gather_outcome(w, m, POLICY, op={"op": "gather_gems", "count": 30, "x": 4 * REGION_SIZE + 2, "y": 3})
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertGreater(out.intents[0]["x"], 40, "east, toward the named region")
        self.assertEqual(m.gather_status, "walking to a target region (region 64,0)")

    def test_unexplored_ground_in_the_named_region_is_a_place_to_go_there(self):
        w = three_regions(at=(24, 8))
        for x in range(REGION_SIZE):
            for y in range(REGION_SIZE):
                w.view.tiles[(x, y)] = "dirt"
        del w.view.tiles[(-1, 8)]  # an opening west, in the named region
        m = Memory()
        gather_outcome(w, m, POLICY, op={"op": "gather_gems", "count": 30, "x": 3, "y": 3})
        self.assertEqual((m.gather_target[0], region_of(m.gather_target[1])), (gather_mod.FRONTIER, (0, 0)))

    def test_with_nothing_known_there_it_works_as_if_none_were_named(self):
        w = three_regions(at=(24, 8))
        for x in range(REGION_SIZE):
            for y in range(REGION_SIZE):
                w.view.tiles[(x, y)] = "dirt"
        out = gather_outcome(w, Memory(), POLICY, op={"op": "gather_gems", "count": 30, "x": 3, "y": 3})
        self.assertEqual(out.reason, "cut grass")

    def piles(self, *cells) -> WorldModel:
        """Dirt over x, y 0..39, walled in, grass at (20, 10) in region (1, 0)."""
        w = fence(grid(["." * 40] * 40, at=(2, 10)))
        w.perception = 30
        w.view.tiles[(20, 10)] = "grass"
        w.entities = [Entity("supply", 50 + i, p, "gem") for i, p in enumerate(cells)]
        return w

    def test_a_pile_outside_the_named_region_is_no_target(self):
        w, m = self.piles((5, 10), (25, 12)), Memory()
        gather_outcome(w, m, POLICY, op={"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(region_of(m.gather_target[1]), (1, 0))

    def test_a_kept_pile_outside_a_newly_named_region_is_let_go(self):
        w, m = self.piles((5, 10)), Memory()
        gather_outcome(w, m, POLICY, op={"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (5, 10)))
        gather_outcome(w, m, POLICY, op={"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(m.gather_target, ("grass", (20, 10)))

    def test_a_pile_beside_it_outside_the_region_is_left_to_pickup(self):
        from agentrealm_agent.states.pickup import PickupState

        w = self.piles((3, 10))
        out = gather_outcome(w, Memory(), POLICY, op={"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertNotEqual(out.intents[0].get("verb"), "Take")
        self.assertTrue(PickupState().guard(w, ctx(w, ["gather_gems:5"], pickup=True)))

    def test_with_nothing_in_the_named_region_piles_anywhere_count(self):
        w = self.piles((5, 10))
        w.view.tiles[(20, 10)] = "dirt"
        m = Memory()
        gather_outcome(w, m, POLICY, op={"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(m.gather_target, ("pile", (5, 10)))


GUARD = ("npc", "fake_gristle")


def remember_still(w: WorldModel, cell, at) -> None:
    """A hostile stood still on ``cell`` (blocked, A162); then it was out of view, its cell out of sight."""
    guard = Entity("npc", 9, cell, GUARD[1])
    w.hostile_types.add(GUARD)
    w.pos = (cell[0] - 3, cell[1])
    w.tick = 0
    w._set_entities([guard], 0)
    w.tick = STILL_TICKS
    w._set_entities([guard], STILL_TICKS)
    w.pos = w.terrain_center = at
    w.tick = STILL_TICKS + 1
    w._set_entities([], w.tick)


class KeepsClearTest(unittest.TestCase):
    """Every walk keeps off known hostile ground, and with every known cell
    to cut barred Gather keeps working: unexplored ground is a place to go."""

    POLICY = Policy(kind="scripted", goals=[], on_hostile="ignore", hostile_range=2, pickup=True)
    GATHER = {"op": "gather_gems", "count": 5}

    def blocked(self) -> WorldModel:
        """Dirt over x 0..39, y 0..20; the only grass lies beside a hostile 13 cells off."""
        w = grid(["." * 40] * 21, at=(12, 10))
        w.perception = 8
        w.alive, w.tick = True, 1000
        for x in (24, 25, 26):
            w.view.tiles[(x, 10)] = "grass"
        w.entities = [Entity("npc", 9, (25, 11), GUARD[1])]
        w.hostile_types.add(GUARD)
        return w

    def test_with_every_cell_to_cut_barred_it_walks_to_unexplored_ground(self):
        w, m = self.blocked(), Memory()
        out = gather_outcome(w, m, self.POLICY, op=self.GATHER)
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "not handed to the safe default")
        kind, cell = m.gather_target
        self.assertEqual(kind, gather_mod.FRONTIER)
        self.assertTrue(gather_ground(w, cell, self.POLICY))
        self.assertTrue(route_clear(w, self.POLICY, m.path, danger(w, self.POLICY)))
        self.assertEqual(m.gather_status, "walking to unexplored ground")

    def test_a_hostile_that_comes_to_hold_its_end_ends_the_walk(self):
        w, m = self.blocked(), Memory()
        gather_outcome(w, m, self.POLICY, op=self.GATHER)
        first = m.gather_target
        w.pos = m.path[0]
        w.entities.append(Entity("npc", 10, first[1], GUARD[1]))
        gather_outcome(w, m, self.POLICY, op=self.GATHER)
        self.assertNotEqual(m.gather_target, first)

    def test_a_taken_first_step_holds_the_walk(self):
        """A peer on the walk's only way out is waited on, not swapped for another target (A71)."""
        w, m = self.blocked(), Memory()
        gather_outcome(w, m, self.POLICY, op=self.GATHER)
        kept, first = m.gather_target, m.path[0]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = (w.pos[0] + dx, w.pos[1] + dy)
                if cell not in (w.pos, first):
                    w.view.tiles[cell] = "wall"
        w.entities.append(Entity("character", 5, first, "peer"))
        m.path, m.goal = [], ""  # another state had the move
        out = gather_outcome(w, m, self.POLICY, op=self.GATHER)
        self.assertTrue(out.wait, out.reason)
        self.assertEqual(m.gather_target, kept)

    def test_with_nowhere_clear_to_go_it_is_blocked(self):
        w, m = fence(self.blocked()), Memory()
        self.assertEqual(w.view.frontier(fords=False), set())
        out = gather_outcome(w, m, self.POLICY, op=self.GATHER)
        self.assertIsNone(out.intents)
        self.assertEqual(m.gather_status, gather_mod.BLOCKED)

    def corridor(self) -> WorldModel:
        """A corridor along y 9..11 that runs past a hostile remembered still
        at (14, 10); the named region (x 32..47) unknown."""
        w = grid(["." * 32] * 21, at=(2, 10))
        remember_still(w, (14, 10), (2, 10))
        for cell in list(w.view.tiles):
            if not 9 <= cell[1] <= 11:
                w.view.tiles[cell] = "wall"
        for x in range(-1, 33):
            w.view.tiles[(x, -1)] = w.view.tiles[(x, 21)] = "wall"
        for y in range(-1, 22):
            w.view.tiles[(-1, y)] = "wall"
        return w

    HELD_GROUND = {(14 + dx, 10 + dy) for dx in range(-3, 4) for dy in range(-3, 4)}

    def test_the_walk_to_an_unseen_region_takes_no_way_past_a_remembered_hostile(self):
        w, m = self.corridor(), Memory()
        w.view.tiles[(4, 10)] = "grass"
        out = gather_outcome(w, m, self.POLICY, op={"op": "gather_gems", "count": 99, "x": 40, "y": 10})
        self.assertFalse(set(m.path) & self.HELD_GROUND, (out.reason, m.path))
        self.assertEqual(m.gather_target, ("grass", (4, 10)), "no way to the region: it works anywhere")
        self.assertEqual(m.gather_status, "walking to grass")
        w.pos = (4, 10)
        self.assertEqual(gather_outcome(w, m, self.POLICY, op={"op": "gather_gems", "count": 99, "x": 40, "y": 10}).reason, "cut grass")

    def test_a_target_picked_with_the_region_lifted_is_kept(self):
        """Commitment holds while Gather works anywhere: the walk is not picked afresh each decision (A71)."""
        w, m = self.corridor(), Memory()
        w.view.tiles[(8, 10)] = "grass"
        op = {"op": "gather_gems", "count": 99, "x": 40, "y": 10}
        gather_outcome(w, m, self.POLICY, op=op)
        kept, walk = m.gather_target, m.gather_walk
        self.assertEqual(kept, ("grass", (8, 10)))
        w.view.tiles[(3, 10)] = "grass"  # nearer grass turns up behind: no reason to let the walk go
        for _ in range(3):
            path = list(m.path)
            with mock.patch.object(gather_mod, "_replan", wraps=gather_mod._replan) as replan:
                out = gather_outcome(w, m, self.POLICY, op=op)
            replan.assert_not_called()
            self.assertEqual((m.gather_target, m.path), (kept, path))
            w.pos = (out.intents[0]["x"], out.intents[0]["y"])
            m.path = m.path[1:]
        self.assertEqual(m.gather_walk, walk, "priced once, on the walk it committed to")

    def test_the_regions_last_grass_in_reach_keeps_the_region(self):
        """Standing on the region's only grass left, with richer grass just outside, it cuts here."""
        w = three_regions(at=(15, 8))
        for x in range(REGION_SIZE):
            for y in range(REGION_SIZE):
                if (x, y) != (15, 8):
                    w.view.tiles[(x, y)] = "dirt"
        kb = KnowledgeBase.empty("fake-world")
        sample(kb, (24, 8), 8, 4)  # the next region over pays 1 in 2
        m = Memory()
        out = gather_outcome(w, m, POLICY, knowledge=kb, op={"op": "gather_gems", "count": 30, "x": 3, "y": 3})
        self.assertEqual(out.reason, "cut grass (region 0,0)")

    def test_a_committed_target_in_the_region_with_no_way_left_lifts_it_at_once(self):
        w = three_regions(at=(24, 8))
        op = {"op": "gather_gems", "count": 30, "x": 3, "y": 3}
        m = Memory()
        gather_outcome(w, m, POLICY, op=op)
        self.assertEqual(region_of(m.gather_target[1]), (0, 0))
        for y in range(-1, REGION_SIZE + 1):
            w.view.tiles[(16, y)] = "wall"  # the region is walled off
        m.path, m.goal = [], ""  # the walk there ran into it: planned again
        out = gather_outcome(w, m, POLICY, op=op)
        self.assertEqual(out.reason, "cut grass", "it works where it is, not handed to the safe default")
        self.assertEqual(m.gather_status, gather_mod.CUTTING, "no region suffix: the bound is lifted")

    def test_the_walk_to_an_unseen_region_takes_the_fight_when_the_op_says(self):
        w, m = self.corridor(), Memory()
        op = {"op": "gather_gems", "count": 99, "x": 40, "y": 10, "fight": True}
        out = gather_outcome(w, m, self.POLICY, op=op)
        self.assertTrue(out.intents, out.reason)
        self.assertTrue(set(m.path) & self.HELD_GROUND, "past the hostile: the op chose the risk")



class ArmAndUsePacingTest(unittest.TestCase):
    """A123: an Arm + Use waits the armed supply's cooldown at the world's tick rate."""

    def test_the_use_waits_what_the_arm_puts_in_hand(self):
        from agentrealm_agent.states.intents import arm_and_use, use_block

        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        w.tick = 100
        m = Memory()
        m.last_use_tick = 99
        for code, hz, cooldown in (("pocket_knife", 10, 8), ("bronze_mallet", 10, 13), ("bronze_mallet", 20, 26)):
            w.held_supplies = [InventorySupply(3, code)]
            queue = arm_and_use(w, m, 3, use_block((1, 0)), hz)
            self.assertEqual(queue[0]["verb"], "Arm", code)
            self.assertEqual(queue[-1]["verb"], "Use", code)
            self.assertEqual(len(queue) - 1, cooldown - 1, (code, hz))  # due a cooldown after the last Use

if __name__ == "__main__":
    unittest.main()
