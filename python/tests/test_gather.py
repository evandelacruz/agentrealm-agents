"""A22: Gather state — grass, bushes, gem piles on known ground with no hostile near."""

import random
import unittest
from unittest import mock

from agentrealm_agent import supplies
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, parse_directives_goal
from agentrealm_agent.states import PlayContext, dispatch, gather_outcome
from agentrealm_agent.states import gather as gather_mod
from agentrealm_agent.hostile_ground import GATHER_HOSTILE_RADIUS, GATHER_SHADOW_MARGIN
from agentrealm_agent.states.gather_safe import gather_ground, is_safe_ish
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.gem_yield import NO_EFFECT_ZONE_CUTS, GemYieldTracker
from agentrealm_agent.zone_discovery import apply_zone


def grid(rows: list[str], at=(1, 1)) -> WorldModel:
    glyph = {".": "dirt", "g": "grass", "b": "bush", "#": "wall", "l": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(ch, "dirt")
    w.terrain_center, w.terrain_map = at, 7
    w.attack_range = 1
    w.gems = 0
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
        """A63 run 1: every zone read was unsafe and Gather never cut."""
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
        """A63 run 3: one following at 4–6 blocks without attacking stopped all cutting."""
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
        """A63 run 4: a 2-cell bar under a longer hostile_range paced Gather and Retreat for 34 s."""
        w = grid(["g" * 12], at=(0, 0))
        w.hostile_types.add(("npc", "gnawer"))
        pol = Policy(hostile=["npc"], hostile_range=3)
        w.entities = [Entity("npc", 1, (4, 0), "gnawer")]
        self.assertFalse(gather_ground(w, (0, 0), pol))
        w.entities = [Entity("npc", 1, (5, 0), "gnawer")]
        self.assertTrue(gather_ground(w, (0, 0), pol))

    def test_gather_never_picks_a_cell_where_retreat_would_fire(self):
        """A63 run 4: after a death Gather and Retreat paced between the safe tile
        and a grass cell 4 times in 34 s with no cut. Hurt to the health floor,
        no cell Gather accepts has Retreat start there, nor after the hostile
        steps once toward it."""
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
        """Breaking a block works in a safe zone: Gather prefers the field, it does not ban safe ground."""
        w = grid(["ggg"], at=(1, 0))
        safe(w, (0, 0))
        self.assertTrue(gather_ground(w, (0, 0), Policy()))

    def test_hazard_and_unknown_cells_disqualify(self):
        w = grid(["gl"], at=(0, 0))
        self.assertFalse(gather_ground(w, (1, 0), Policy(avoid_blocks=["lava"])))
        self.assertFalse(gather_ground(w, (5, 5), Policy()))


class SafeIshTest(unittest.TestCase):
    """The stricter safe-zone ground a hurt character walks first."""

    def test_known_safe_tile_qualifies(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        self.assertTrue(is_safe_ish(w, (1, 0), Policy()))

    def test_hostile_in_range_disqualifies(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        w.entities = [Entity("npc", 1, (2, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        pol = Policy(hostile=["npc"], hostile_range=2)
        self.assertFalse(is_safe_ish(w, (1, 0), pol))

    def test_townsfolk_in_range_do_not_disqualify(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        w.entities = [Entity("npc", 1, (2, 0), "villager")]
        self.assertTrue(is_safe_ish(w, (1, 0), Policy(hostile=["npc"], hostile_range=2)))

    def test_respawn_ring_qualifies_without_zone(self):
        w = grid(["ggg"], at=(2, 0))
        w.record_respawn_anchor(7, (0, 0))
        self.assertTrue(is_safe_ish(w, (2, 0), Policy()))


class GatherActTest(unittest.TestCase):
    def test_field_grass_before_safe_grass(self):
        """A63 run 2: 168 cuts of one town grass cell, every one applied_no_effect."""
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(m.gather_target, ("grass", (0, 0)))

    def test_a_bush_is_never_cut(self):
        """A81: bushes drop berries, not gems."""
        w = grid([".b."], at=(0, 0))
        out = outcome(w)
        self.assertFalse(any(i.get("verb") == "Use" for i in out.intents or []))

    def test_unlisted_supply_codes_are_not_treated_as_gem_piles(self):
        w = grid(["...", "..."], at=(1, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem_cache_5")]
        self.assertIsNone(outcome(w, pickup=False).intents)

    def test_priced_gem_supply_is_shop_stock_not_a_pile(self):
        w = grid(["...", "..."], at=(1, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem", gem_price=3)]
        self.assertIsNone(outcome(w, pickup=False).intents)

    def test_takes_gem_pile_on_safe_ground(self):
        """Piles are not cuts: free ones on safe ground are taken at once."""
        w = grid(["g.g"], at=(1, 0))
        safe(w, (1, 0), (2, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem")]
        self.assertEqual(outcome(w, pickup=False).intents[0]["verb"], "Take")

    def test_takes_gem_pile_in_range(self):
        w = grid(["g.g"], at=(1, 0))
        safe(w, (1, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem")]
        out = outcome(w, pickup=False)
        self.assertEqual(out.intents[0]["verb"], "Take")

    def test_cuts_field_grass_outside_safe_zones(self):
        w = grid(["ggg"], at=(1, 0))
        self.assertEqual(outcome(w).reason, "cut grass")

    def test_skips_grass_with_a_hostile_near(self):
        w = grid(["ggg"], at=(1, 0))
        w.entities = [Entity("npc", 4, (2, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        self.assertIsNone(outcome(w, hostile=["npc"]).intents)


class GatherArmsACutterTest(unittest.TestCase):
    """Free-play run 3: Gather cut with a potion armed, and the no-effect cuts
    marked the ground uncuttable."""

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
        """Review on #152: a mallet swapped for the knife was never put back,
        so every later fight used the knife."""
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

    def test_with_nothing_that_cuts_it_cuts_with_what_is_in_hand(self):
        w = grid(["ggg"], at=(1, 0))
        w.armed_code = "fake_cleaver"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        self.assertEqual(outcome(w).intents, [{"verb": "Use", "target": {"kind": "block", "x": 1, "y": 0}}])


class GatherPathingTest(unittest.TestCase):
    def test_walks_to_field_grass(self):
        w = grid(["..g"], at=(0, 0))
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents[0], {"verb": "SetPosition", "x": 1, "y": 0})
        self.assertEqual((m.goal, m.gather_target), ("gather", ("grass", (2, 0))))

    def test_walks_past_a_nearer_bush_to_grass(self):
        """A81: a bush beside us is no gem source; the grass further off is."""
        w = grid(["g..b"], at=(2, 0))
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (0, 0)))

    def test_walks_to_a_known_pile_first(self):
        w = grid(["g...."], at=(1, 0))
        safe(w, (0, 0), (4, 0))
        w.entities = [Entity("supply", 9, (4, 0), "gem")]
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("pile", (4, 0)))
        self.assertEqual(m.path[-1], (4, 0))

    def test_pile_target_path_is_kept_between_ticks(self):
        w = grid(["......"], at=(0, 0))
        safe(w, (5, 0))
        w.entities = [Entity("supply", 9, (5, 0), "gem")]
        m = Memory()
        outcome(w, m)
        path = list(m.path)
        with mock.patch.object(gather_mod, "_replan_gather") as replan:
            outcome(w, m)
        replan.assert_not_called()
        self.assertEqual(m.path, path)

    def test_target_that_stops_qualifying_is_dropped(self):
        w = grid(["...g"], at=(0, 0))
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (3, 0)))
        w.view.tiles[(3, 0)] = "dirt"  # cut by someone else
        out = outcome(w, m)
        self.assertIsNone(out.intents)
        self.assertEqual((m.path, m.goal, m.gather_target), ([], "", None))

    def test_no_target_keeps_another_states_path(self):
        w = grid(["...."], at=(0, 0))
        m = Memory(path=[(1, 0), (2, 0)], goal="explore")
        self.assertIsNone(outcome(w, m).intents)
        self.assertEqual((m.path, m.goal), ([(1, 0), (2, 0)], "explore"))


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
        from agentrealm_agent.directives import PARAM_DEFAULTS

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


class GatherHeadsOutTest(unittest.TestCase):
    """On safe ground with nothing to cut: walk out to the field (A63 run 2)."""

    def test_cuts_safe_grass_when_no_field_cell_is_known(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (0, 0), (1, 0), (2, 0))
        self.assertEqual(outcome(w).reason, "cut grass")

    def test_heads_to_the_nearest_field_cell(self):
        w = grid(["........"], at=(0, 0))
        safe(w, *[(x, 0) for x in range(5)])
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(c.memory.gather_target, (gather_mod.OUT, (5, 0)))
        self.assertEqual(c.memory.gather_status, gather_mod.HEADING_OUT)

    def test_heads_to_the_frontier_when_no_field_cell_is_known(self):
        w = grid(["...."], at=(0, 0))
        safe(w, *[(x, 0) for x in range(4)])
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(m.gather_target[0], gather_mod.OUT)

    def test_no_effect_cuts_mark_the_safe_zone_uncuttable_and_it_heads_out(self):
        w = grid(["ggggg..."], at=(0, 0))
        safe(w, *[(x, 0) for x in range(5)])
        tracker, m = GemYieldTracker(), Memory()
        for x in range(NO_EFFECT_ZONE_CUTS - 1):
            tracker.note_no_effect(w, (x, 0), "grass", w.tick)
        out = gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_target, ("grass", (NO_EFFECT_ZONE_CUTS - 1, 0)), "below the bar: the rest stay cuttable")
        self.assertEqual(m.gather_status, gather_mod.NO_EFFECT)
        tracker.note_no_effect(w, (NO_EFFECT_ZONE_CUTS - 1, 0), "grass", w.tick)
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"), gem_cuts=tracker)
        self.assertEqual(m.gather_target, (gather_mod.OUT, (5, 0)), "the whole zone is uncuttable")
        self.assertEqual(m.gather_status, gather_mod.HEADING_OUT, "heading out outranks a no-effect status")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_grass_found_on_arrival_ends_the_walk_out(self):
        """Review on #126: a cut returned early left the OUT target and HEADING_OUT status behind."""
        w = grid(["........"], at=(0, 0))
        safe(w, *[(x, 0) for x in range(5)])
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_status, gather_mod.HEADING_OUT)
        w.pos = (5, 0)
        w.view.tiles[(5, 0)] = "grass"
        out = outcome(w, m)
        self.assertEqual(out.reason, "cut grass")
        self.assertEqual((m.goal, m.gather_target), ("", None))
        self.assertEqual(m.gather_status, gather_mod.CUTTING)

    def test_out_walk_is_kept_while_the_grass_stays_unreachable(self):
        """Review on #126: walled-off grass dropped the walk out every tick, replanning it (A15)."""
        w = grid(["......###", "......#g#", "......###"], at=(0, 1))
        safe(w, *[(x, y) for x in range(5) for y in range(3)])
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target[0], gather_mod.OUT)
        path = list(m.path)
        with mock.patch.object(gather_mod, "_replan_gather") as replan:
            outcome(w, m)
        replan.assert_not_called()
        self.assertEqual(m.path, path)

    def test_off_safe_ground_the_out_walk_is_dropped_for_field_grass(self):
        w = grid([".....gg"], at=(0, 0))
        safe(w, *[(x, 0) for x in range(5)])
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (5, 0)))
        w.view.tiles[(5, 0)] = w.view.tiles[(6, 0)] = "dirt"
        m.path, m.goal, m.gather_target = [], "", None
        outcome(w, m)
        self.assertEqual(m.gather_target, (gather_mod.OUT, (5, 0)))
        w.pos = (5, 0)
        w.view.tiles[(6, 0)] = "grass"
        out = outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (6, 0)))
        self.assertEqual(out.intents[0]["verb"], "SetPosition")


class GatherFallbackTest(unittest.TestCase):
    """Nothing to cut in reach: walk to known cuttable ground before exploring (A63 run 1)."""

    def test_walks_to_known_grass_far_away_rather_than_exploring(self):
        w = grid(["." * 30 + "g"], at=(0, 0))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(c.memory.gather_target, ("grass", (30, 0)))
        self.assertEqual(c.memory.gather_status, "walking to grass", "a walk is not a cut (A63 run 3)")

    def test_explores_when_no_known_cell_is_cuttable(self):
        w = grid([".....", "....."], at=(0, 0))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertTrue(out.reason.startswith("look for gems: "))
        self.assertEqual(c.memory.gather_status, gather_mod.NONE_CUTTABLE)

    def test_standing_in_a_barren_region_says_so(self):
        w = grid(["ggg"], at=(1, 0))
        c = ctx(w, ["gather_gems:3"])
        with mock.patch.object(gather_mod, "barren_regions", return_value={(0, 0)}):
            dispatch(w, c)
        self.assertEqual(c.memory.gather_status, gather_mod.REGION_BARREN)


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

    def test_no_target_in_sight_explores_itself(self):
        w = grid(["....", "....", "...."], at=(1, 1))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertIsNotNone(out.intents, "Gather moves instead of the character standing still")
        self.assertTrue(out.reason.startswith("look for gems: "), out.reason)
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

    def test_unreachable_target_explores_instead(self):
        # Grass walled off: no path to it, so Gather explores safe ground itself.
        w = grid(["....###", "....#g#", "....###"], at=(1, 1))
        safe(w, (5, 1))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertIsNotNone(out.intents)
        self.assertTrue(out.reason.startswith("look for gems: "), out.reason)


if __name__ == "__main__":
    unittest.main()


class GatherShadowTest(unittest.TestCase):
    """A63 run 3: a hostile followed at 4–6 blocks for 40 s without attacking; Gather inched away and cut nothing."""

    def shadowed(self, on_hostile: str, at=(10, 0)) -> tuple[WorldModel, PlayContext]:
        w = grid(["g" * 40], at=at)
        w.entities = [Entity("npc", 9, (at[0] + 4, 0), "wartlurch")]
        w.hostile_types.add(("npc", "wartlurch"))  # a type seen attacking (survival.is_hostile)
        c = ctx(w, ["gather_gems:3"], on_hostile=on_hostile, hostile=["npc"], hostile_range=2)
        return w, c

    def follow(self, w: WorldModel, c: PlayContext, seconds: int) -> None:
        """Gather decides every second for ``seconds`` with the hostile still near."""
        dispatch(w, c)
        for _ in range(seconds):
            w.tick += c.plan.tick_hz
            gather_mod.shadowing_hostile(w, c.memory, c.policy, c.plan.tick_hz)

    def test_a_hit_from_the_shadow_restarts_its_clock(self):
        w, c = self.shadowed("flee")
        self.follow(w, c, gather_mod.SHADOW_SECONDS - 5)
        w.attacker, w.attacked_tick = ("npc", 9), w.tick  # it hits us
        self.follow(w, c, 10)
        w.attacker = None  # the hit has lapsed from threat memory
        self.assertIsNone(gather_mod.shadowing_hostile(w, c.memory, c.policy, c.plan.tick_hz),
                          "only 10 s without hitting us since the hit")

    def test_a_gap_restarts_its_clock(self):
        w, c = self.shadowed("flee")
        self.follow(w, c, gather_mod.SHADOW_SECONDS - 1)
        w.tick += gather_mod.SHADOW_GAP_SECONDS * c.plan.tick_hz  # another state ran meanwhile
        self.assertIsNone(gather_mod.shadowing_hostile(w, c.memory, c.policy, c.plan.tick_hz))
        self.assertEqual(c.memory.gather_shadow, (9, w.tick, w.tick))

    def test_an_npc_of_a_type_never_seen_attacking_is_no_shadow(self):
        w, c = self.shadowed("fight")
        w.hostile_types.clear()  # townsfolk standing near (survival.is_hostile)
        self.follow(w, c, gather_mod.SHADOW_SECONDS)
        self.assertEqual(dispatch(w, c).intents[0]["verb"], "Use")

    def test_cuts_beside_a_hostile_that_has_not_attacked(self):
        w, c = self.shadowed("flee")
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.intents[0]["verb"], "Use", "4 blocks off and harmless: keep cutting")
        self.assertEqual(c.memory.gather_status, gather_mod.CUTTING)

    def test_moves_well_off_from_a_long_shadow_in_one_walk(self):
        w, c = self.shadowed("flee")
        self.follow(w, c, gather_mod.SHADOW_SECONDS)
        out = dispatch(w, c)
        kind, goal = c.memory.gather_target
        self.assertEqual(kind, gather_mod.OFF)
        self.assertGreaterEqual(abs(goal[0] - 14), gather_mod.MOVE_OFF_DISTANCE, "not the nearest uncovered cell")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(c.memory.gather_status, gather_mod.MOVING_OFF)
        # The next decision keeps the same walk, not a fresh nearest cell.
        w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        dispatch(w, c)
        self.assertEqual(c.memory.gather_target, (gather_mod.OFF, goal))

    def test_fights_a_long_shadow_when_the_profile_fights_and_wins(self):
        w, c = self.shadowed("fight")
        self.follow(w, c, gather_mod.SHADOW_SECONDS)
        with mock.patch.object(gather_mod, "would_fight", return_value=True):
            out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "closes on it")
        self.assertIn("npc 9 shadows us", out.reason)
        self.assertEqual(c.memory.gather_status, gather_mod.FIGHTING)

    def test_moves_off_instead_when_the_fight_would_be_lost(self):
        w, c = self.shadowed("fight")
        self.follow(w, c, gather_mod.SHADOW_SECONDS)
        with mock.patch.object(gather_mod, "would_fight", return_value=False):
            dispatch(w, c)
        self.assertEqual(c.memory.gather_status, gather_mod.MOVING_OFF)

    def test_a_short_shadow_changes_nothing(self):
        w, c = self.shadowed("fight")
        self.follow(w, c, gather_mod.SHADOW_SECONDS - 1)
        out = dispatch(w, c)
        self.assertEqual(out.intents[0]["verb"], "Use")


class GatherStatusTest(unittest.TestCase):
    """A63 run 3: gather_status said "cutting" through 45 s of walking with no cut."""

    def test_blocked_by_hostile_when_only_a_hostile_bars_the_grass(self):
        w = grid(["..g"], at=(0, 0))
        w.entities = [Entity("npc", 9, (2, 0), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))
        m = Memory()
        out = outcome(w, m, hostile=["npc"])
        self.assertIsNone(out.intents)
        self.assertEqual(m.gather_status, gather_mod.BLOCKED)

    def test_flags_no_cut_for_a_while(self):
        w = grid(["." * 30 + "g"], at=(0, 0))
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
