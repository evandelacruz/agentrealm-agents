"""A10: Heal state and healing helpers."""

import random
import unittest
import unittest.mock

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.executor import DEFAULT_WEAPON_COOLDOWN_TICKS
from agentrealm_agent.healing import absorb_heal_pending, hurt, note_heal_pending
from agentrealm_agent.executor.supply_refusals import Refusal, note_refusal, refusal_situation
from agentrealm_agent.item_table import InventorySupply, carried_from_inventory
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def grid(at=(1, 1), size=5) -> WorldModel:
    """Map 7, all dirt, safe zone at (0, 0) beside the respawn anchor."""
    w = WorldModel(character_id=9, map_id=7, pos=at, perception=size, health=5, max_health=10)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True)}
    if at != (0, 0):
        w.zones[7][at] = ZoneFact(safe=False)
    return w


def ctx(m: Memory | None = None, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m or Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)


def verbs(out) -> list[str]:
    return [i["verb"] for i in out.intents or []]


class HealingHelpersTest(unittest.TestCase):
    def test_hurt(self):
        self.assertTrue(hurt(grid()))

    def test_held_from_carried_inventory(self):
        inv = {"held": [{"id": 4, "supply_subtype_code": "small_potion"}, {"id": "x"}, "junk"]}
        self.assertEqual(carried_from_inventory(inv)[0], [InventorySupply(4, "small_potion")])

    def test_apply_observation_fills_held(self):
        w = WorldModel(character_id=9)
        inv = {"held": [{"id": 4, "supply_subtype_code": "apple"}], "armed": None}
        w.apply_observation({"complete": True, "snapshot": {"health": 5, "max_health": 10, "inventory": inv}})
        self.assertEqual(w.held_supplies, [InventorySupply(4, "apple")])

class HealStateTest(unittest.TestCase):
    def test_takes_adjacent_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])

    def test_walks_one_step_toward_distant_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (4, 4), "golden_cap")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 2, "y": 2}])

    def test_a_held_take_waits_for_the_situation_to_change(self):
        # The runner files each refusal (``Runner._note_supply_refused``).
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Take"])
        note_refusal(m, "take", 8, Refusal("odd_code", "hold", w.tick, refusal_situation(w, (2, 1))))
        w.tick += 7
        self.assertNotIn("Take", verbs(dispatch(w, ctx(m))), "nothing changed: not sent again")
        w.held_supplies = [InventorySupply(3, "bronze_sword")]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Take"], "a new item: try again")

    def test_carried_food_before_potion(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(5, "berry")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 5})
        self.assertEqual(verbs(out), ["Arm", "Use"])

    def test_drinks_carried_potion_and_holds_a_refused_one(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        note_refusal(m, "use", 4, Refusal("odd_code", "hold", w.tick, refusal_situation(w)))
        w.tick += 7
        self.assertNotIn("Use", verbs(dispatch(w, ctx(m))))

    def test_rearms_weapon_after_drinking_potion(self):
        w = grid()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(verbs(out), ["Arm", "Use"])
        self.assertEqual(m.self_use_rearm, "pocket_knife")
        w.armed_code = None
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        out = dispatch(w, ctx(m))
        self.assertEqual(verbs(out), ["Arm"])
        self.assertIsNone(m.self_use_rearm)

    def test_rearms_weapon_once_the_drink_heals_to_full(self):
        # A potion that fills health ends the hurt guard; the re-arm still runs.
        w = grid()
        w.armed_code = "small_potion"
        w.health = 10
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        m = Memory(self_use_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])
        self.assertIsNone(m.self_use_rearm)
        w.armed_code = "pocket_knife"
        self.assertNotEqual(dispatch(w, ctx(m)).state, "Heal")

    def test_rearms_weapon_with_a_hostile_in_range(self):
        w = grid()
        w.health = 10
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        w.entities = [Entity("npc", 3, (2, 2), "slime")]
        m = Memory(self_use_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])

    def test_the_weapon_goes_back_before_a_second_potion(self):
        # Free-play run 3: self_use_rearm held from 191 s to the end. It never
        # outlives one drink now, so Equip is never kept waiting on it.
        w = grid()
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        m = Memory(self_use_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])
        self.assertIsNone(m.self_use_rearm)
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertEqual(m.self_use_rearm, "pocket_knife")

    def test_a_drink_sends_the_use_with_the_arm(self):
        """Free-play run 3: the drink's Arm went out and its Use never did."""
        w = grid()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        d = decide(w, Memory(), Policy(kind="scripted"), random.Random(0))
        self.assertEqual(d.state, "Heal")
        self.assertEqual(d.submit_queue, [{"verb": "Arm", "supply_id": 4},
                                          {"verb": "Use", "target": {"kind": "self"}}])

    def test_a_drink_waits_out_the_use_cooldown_after_the_arm(self):
        w = grid()
        w.tick = 100
        w.held_supplies = [InventorySupply(4, "small_potion")]
        out = dispatch(w, ctx(Memory(last_use_tick=96)))
        self.assertTrue(out.paced)
        self.assertEqual(verbs(out), ["Arm"] + ["Wait"] * (DEFAULT_WEAPON_COOLDOWN_TICKS - 5) + ["Use"])

    def test_the_reflex_probe_leaves_the_rearm_due(self):
        # While the drink's queue is held, the runner's reflex probe runs
        # Heal; the re-arm must still be due when the queue is done.
        import threading
        from pathlib import Path

        from agentrealm_agent.config import CharacterConfig
        from agentrealm_agent.runner import Runner

        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = grid()
        r.world.armed_code = "small_potion"
        r.world.held_supplies = [InventorySupply(1, "pocket_knife")]
        r.mem = Memory(need_self=False, need_position=False, self_use_rearm="pocket_knife")
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual(r.mem.self_use_rearm, "pocket_knife")

    def test_a_drink_with_nothing_armed_leaves_nothing_to_rearm(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertIsNone(m.self_use_rearm)

    def test_weapon_gone_clears_rearm(self):
        w = grid()
        w.health = 10
        m = Memory(self_use_rearm="pocket_knife")
        self.assertNotEqual(dispatch(w, ctx(m)).state, "Heal")
        self.assertIsNone(m.self_use_rearm)

    def test_takes_food_even_when_it_would_overheal(self):
        # Free food beats a potion or a walk to town, whatever its heal.
        w = grid(at=(1, 1))
        w.health = 9
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        w.held_supplies = [InventorySupply(4, "small_potion")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["apple"] = {"heal_amount": 5}
        out = dispatch(w, ctx(kb=kb))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])

    def test_learns_heal_from_applied_take(self):
        kb = KnowledgeBase.empty("sandbox")
        m = Memory(heal_pending=(5, "apple", "take"))
        w = grid()
        w.health = 8
        absorb_heal_pending(m, w, kb, [])
        self.assertEqual(kb.items["apple"]["heal_amount"], 3)
        self.assertTrue(kb.items["apple"]["heal_on_pickup"])

    def test_a_drink_behind_cooldown_waits_is_measured(self):
        # The potion is named by the Arm before the Use, Waits aside.
        from agentrealm_agent.runner import Runner

        r = Runner.__new__(Runner)
        r.world, r.mem = grid(), Memory()
        r.world.armed_code = "pocket_knife"
        r.world.held_supplies = [InventorySupply(4, "small_potion")]
        use = {"verb": "Use", "target": {"kind": "self"}}
        r.mem.pending_intents = [{"verb": "Arm", "supply_id": 4}, {"verb": "Wait"}, {"verb": "Wait"}, use]
        r._note_heal_intent(use, 3)
        self.assertEqual(r.mem.heal_pending, (5, "small_potion", "use"))

    def test_nothing_learned_at_full_health(self):
        # Loot also Takes food; at full health it cannot show a heal.
        m, w = Memory(), grid()
        w.health = 10
        note_heal_pending(m, w, "apple", "take")
        self.assertIsNone(m.heal_pending)

    def test_hostile_in_range_keeps_heal_out(self):
        # Retreat or Flee (A9) answers the hostile; Heal waits until none is in range.
        w = grid()
        w.entities = [Entity("npc", 3, (2, 2), "slime")]
        w.hostile_types.add(("npc", "slime"))  # a type seen attacking (survival.is_hostile)
        self.assertIn(dispatch(w, ctx()).state, ("Retreat", "Flee"))

    def test_full_health_yields_to_explore(self):
        w = grid()
        w.health = 10
        self.assertEqual(dispatch(w, ctx()).state, "Explore")

    def test_heal_supplies_asked_again_after_a_full_heal(self):
        w = grid(at=(2, 2))
        m = Memory()
        dispatch(w, ctx(m))
        w.health = 10
        dispatch(w, ctx(m))
        w.health = 4
        dispatch(w, ctx(m))
        self.assertEqual(len([s for s in m.strategist_signals if s["trigger"] == "heal_supplies"]), 2)

    def test_heal_supplies_rearmed_by_a_full_heal_another_state_saw(self):
        # Review: a full heal seen while Fight or Flee held the round re-arms the ask.
        from agentrealm_agent.healing import note_heal_window

        m = Memory(heal_supplies_asked=True)
        w = grid()
        w.health = 10
        note_heal_window(m, w)
        self.assertFalse(m.heal_supplies_asked)
        m.heal_supplies_asked = True
        w.health = 4
        note_heal_window(m, w)
        self.assertTrue(m.heal_supplies_asked)

    def test_dispatch_rearms_the_ask_even_when_a_reflex_above_heal_runs(self):
        m = Memory(heal_supplies_asked=True)
        w = grid()
        w.health = 10
        w.entities = [Entity("npc", 3, (2, 2), "slime")]  # Retreat or Flee holds the round
        out = dispatch(w, ctx(m))
        self.assertNotEqual(out.state, "Heal")
        self.assertFalse(m.heal_supplies_asked)

    def test_nothing_to_heal_with_never_walks_to_safe_ground_or_waits(self):
        # A130: the game has no natural regeneration, so with no food and no
        # potion due Heal sends nothing, off safe ground or on it, at any
        # health: no walk to safe ground, no Wait to measure or rest. It
        # asks the planner once for food and potions; the safe default moves.
        m = Memory()
        for at, health, tick in (((2, 2), 5, 0), ((2, 2), 1, 10), ((0, 0), 1, 20), ((0, 0), 1, 400)):
            w = grid(at=at)
            w.health, w.tick = health, tick
            out = dispatch(w, ctx(m))
            self.assertNotEqual(out.state, "Heal", out.reason)
            self.assertIn("Heal: nothing to heal with", out.yielded)
            self.assertNotIn("Wait", verbs(out))
        asks = [s for s in m.strategist_signals if s["trigger"] == "heal_supplies"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(set(asks[0]), {"trigger", "health", "max_health", "potions", "tick"})
        self.assertEqual(m.nav_stuck.given_up, {})


class RearmTheLastWeaponTest(unittest.TestCase):
    """Free-play run 4: a potion left armed by run 3 was what Heal remembered
    to re-arm, so Heal re-armed a potion and no weapon came back."""

    def test_drinking_the_armed_potion_rearms_the_held_weapon(self):
        w = grid()
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "bronze_sword"), InventorySupply(4, "small_potion")]
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "self"}}])
        self.assertEqual(m.self_use_rearm, "bronze_sword")
        w.armed_code = None
        w.held_supplies = [InventorySupply(1, "bronze_sword"), InventorySupply(4, "small_potion")]
        self.assertEqual(dispatch(w, ctx(m)).intents, [{"verb": "Arm", "supply_id": 1}])

    def test_a_potion_in_the_slot_is_never_the_weapon_to_rearm(self):
        w = grid()
        w.armed_code = "large_potion"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertIsNone(m.self_use_rearm, "no weapon held: nothing to re-arm, never the potion")

    def test_the_last_weapon_armed_comes_back_over_another_held_one(self):
        w = grid()
        w.health = 10
        w.armed_code = "bronze_sword"
        m = Memory()
        dispatch(w, ctx(m))  # every decision notes the weapon in hand
        self.assertEqual(m.last_weapon, "bronze_sword")
        w.health = 5
        w.armed_code = "torch"  # a tool swapped in since
        w.held_supplies = [
            InventorySupply(1, "pocket_knife"),
            InventorySupply(2, "bronze_sword"),
            InventorySupply(4, "small_potion"),
        ]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertEqual(m.self_use_rearm, "bronze_sword")


class DrinkTargetsSelfTest(unittest.TestCase):
    """Free-play run 4: the Use named our own id as a ``character`` target
    and drank nothing. The API's target for yourself is ``{"kind": "self"}``."""

    def test_the_drink_uses_the_self_target(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents[-1], {"verb": "Use", "target": {"kind": "self"}})

    def test_a_self_use_is_measured(self):
        from agentrealm_agent.runner import Runner

        r = Runner.__new__(Runner)
        r.world, r.mem = grid(), Memory()
        r.world.armed_code = "small_potion"
        use = {"verb": "Use", "target": {"kind": "self"}}
        r.mem.pending_intents = [use]
        r._note_heal_intent(use, 0)
        self.assertEqual(r.mem.heal_pending, (5, "small_potion", "use"))



# The health line (A89), on open ground with a cluster of safe cells west of
# the character.
CLUSTER = [(4, 10), (4, 11), (3, 10), (3, 11)]
TRAVEL = {"op": "travel", "to": "point", "x": 30, "y": 10}


def open_ground(health: int):
    from tests.test_survival_runs import world

    return world(health=health)


def line_ctx(plan=None, **params) -> PlayContext:
    from agentrealm_agent.directives import PARAM_DEFAULTS

    policy = Policy(kind="scripted", goals=[], hostile=["npc"])
    return PlayContext(Memory(), policy, random.Random(0), params={**PARAM_DEFAULTS, **params}, plan=plan)


def travel_plan():
    from agentrealm_agent.directives import PARAM_DEFAULTS
    from agentrealm_agent.plan import Plan

    return Plan([dict(TRAVEL)], dict(PARAM_DEFAULTS))


class LowHealthLineTest(unittest.TestCase):
    """The planner's ``low_health`` line says when a carried potion is spent.
    Health never comes back by resting (A130), so no line pulls the character
    off the plan's op to safe ground."""

    def test_at_any_health_the_plans_op_goes_on_with_nothing_to_heal_with(self):
        from tests.test_freeplay_run3 import safe

        for health in (9, 4, 1):
            w, c = open_ground(health), line_ctx(travel_plan())
            safe(w, *CLUSTER)
            out = dispatch(w, c)
            self.assertEqual(out.state, "Travel", health)
            self.assertIn("Heal: nothing to heal with", out.yielded)

    def test_with_no_plan_op_heal_starts_no_walk_to_safe_ground(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(4), line_ctx()
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Heal")
        self.assertIsNone(targets_committed(c.memory, w))

    def test_the_line_says_when_a_carried_potion_is_drunk(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(9), line_ctx(travel_plan())
        safe(w, *CLUSTER)
        w.held_supplies = [InventorySupply(4, "small_potion")]
        self.assertEqual(dispatch(w, c).state, "Travel", "above the line the potion is kept")
        w, c = open_ground(9), line_ctx(travel_plan(), low_health=0.9)
        w.held_supplies = [InventorySupply(4, "small_potion")]
        out = dispatch(w, c)
        self.assertEqual(out.state, "Heal")
        self.assertIn({"verb": "Use", "target": {"kind": "self"}}, out.intents)

    def test_safe_ground_stays_the_refuge_from_attack(self):
        # A130: Heal no longer walks there, but Retreat still does, under attack.
        from tests.test_freeplay_run3 import safe
        from tests.test_survival_runs import ctx as fight_ctx, hit

        w, c = open_ground(4), fight_ctx(on_hostile="fight")
        safe(w, *CLUSTER)
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {CLUSTER[0]}"))

    def test_the_planner_may_only_raise_the_line(self):
        # A lower line drinks later: a loosening, like any survival param.
        from agentrealm_agent.directives import PARAM_DEFAULTS
        from agentrealm_agent.plan import apply_set_param, collect_rejections, validate_goal_op

        raised = {"op": "set_param", "name": "low_health", "value": 0.8}
        self.assertEqual(apply_set_param(dict(PARAM_DEFAULTS), dict(PARAM_DEFAULTS), raised)["low_health"], 0.8)
        lowered = {"op": "set_param", "name": "low_health", "value": 0.2}
        params, rejected = collect_rejections(lambda: apply_set_param(dict(PARAM_DEFAULTS), dict(PARAM_DEFAULTS), lowered))
        self.assertEqual(params["low_health"], PARAM_DEFAULTS["low_health"])
        self.assertTrue(rejected)
        self.assertIsNone(validate_goal_op({"op": "set_param", "name": "low_health", "value": 1.5}))


def targets_committed(m: Memory, w: WorldModel):
    """Heal's only commitment, the food it walks to; no safe cell is ever one."""
    from agentrealm_agent import targets

    return targets.committed(m, w, "heal_food")


def ringed(w: WorldModel, size: int) -> WorldModel:
    """A wall ring round the ``size`` grid: no way round through unknown ground."""
    for i in range(-1, size + 1):
        for p in ((i, -1), (i, size), (-1, i), (size, i)):
            w.view.tiles[p] = "wall"
    return w


class FoodByPriceTest(unittest.TestCase):
    """Food within ``FOOD_REACH`` is picked by one price: its walk on Heal's
    ground, a block on the straight way a held item opens costed at its break."""

    def test_the_cheapest_walk_wins_not_the_nearest_cell(self):
        w = grid(at=(3, 4), size=8)
        w.zones[7] = {}
        for x in range(0, 7):
            w.view.tiles[(x, 3)] = "wall"  # the near food is behind a wall, the way round is long
        w.entities = [Entity("supply", 1, (3, 2), "apple"), Entity("supply", 2, (6, 6), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.reason, "heal_food → (6, 6)")

    def test_food_behind_a_bush_a_held_blade_cuts(self):
        w = ringed(grid(at=(1, 2), size=6), 6)
        w.zones[7] = {}
        for y in range(6):
            w.view.tiles[(2, y)] = "bush"  # no way round: the bush is the way
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.entities = [Entity("supply", 1, (3, 2), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertTrue(out.reason.startswith("heal_food: break cut @ (2, 2)"), out.reason)
        self.assertIn({"verb": "Use", "target": {"kind": "block", "x": 2, "y": 2}}, out.intents)

    def test_a_walk_round_cheaper_than_the_cut_is_walked(self):
        w = grid(at=(1, 2), size=6)
        w.zones[7] = {}
        w.view.tiles[(2, 2)] = "bush"  # one bush, open ground round it
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.entities = [Entity("supply", 1, (3, 2), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (3, 2)"))

    def test_food_no_walk_reaches_is_no_target(self):
        w = ringed(grid(at=(1, 2), size=6), 6)
        w.zones[7] = {}
        for y in range(6):
            w.view.tiles[(2, y)] = "wall"
        w.entities = [Entity("supply", 1, (3, 2), "apple")]
        out = dispatch(w, ctx())
        self.assertNotEqual(out.state, "Heal")


class HealWalkOpensItsPricedWayTest(unittest.TestCase):
    """The block Heal opens is the first one its priced way crosses, on
    every Heal walk, food or not."""

    def test_the_block_opened_is_the_one_the_way_crosses(self):
        # Two bushes on the straight route; the cheapest way goes round the
        # first (open ground above it) and cuts the second.
        w = ringed(grid(at=(0, 2), size=6), 6)
        w.zones[7] = {}
        w.view.tiles[(1, 2)] = "bush"
        for y in range(6):
            w.view.tiles[(3, y)] = "bush"  # a full column: the way must cut one
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.entities = [Entity("supply", 1, (5, 2), "apple")]
        from agentrealm_agent.states import heal

        c = ctx()
        with unittest.mock.patch.object(heal, "FOOD_REACH", 5):
            out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (5, 2)"))
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 1}], "round the first bush")
        self.assertIn((3, 2), c.memory.path, "the walk crosses the block its price cut")
        self.assertEqual(c.memory.nav_stuck.stuck_signals, [], "food priced reachable is never given up")
        # Beside the block, Heal opens it.
        w.pos = c.memory.path[c.memory.path.index((3, 2)) - 1]
        with unittest.mock.patch.object(heal, "FOOD_REACH", 5):
            out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food: break cut @ (3, 2)"))

    def test_the_walk_to_a_block_out_of_reach_is_heals_own(self):
        # The straight way to the bush column runs through a pack's reach; a
        # longer way goes round it. Out of the blade's reach, the walk to the
        # block is Heal's: on its ground (the reach costly), bounded by stuck
        # detection, and its goal kept, so a hostile seen later replans it.
        from agentrealm_agent.states import heal
        from agentrealm_agent.survival import hostile_reach

        w = ringed(grid(at=(0, 0), size=11), 11)
        w.zones[7] = {}
        for y in range(11):
            w.view.tiles[(8, y)] = "bush"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.hostile_types.add(("npc", "wartlurch"))
        w.entities = [Entity("supply", 1, (10, 0), "apple"), Entity("npc", 20, (3, 1), "wartlurch")]
        c = ctx()
        with unittest.mock.patch.object(heal, "FOOD_REACH", 10):
            out = dispatch(w, c)
        m = c.memory
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (10, 0)"))
        self.assertEqual(m.goal, "heal_food")
        self.assertIn((8, 0), m.path, "the way cuts the bush its price chose")
        self.assertTrue(hostile_reach(w, c.policy).isdisjoint(m.path), m.path)
        self.assertIsNotNone(heal.nav_stuck.active(m, w), "the walk is a bounded attempt")


if __name__ == "__main__":
    unittest.main()
