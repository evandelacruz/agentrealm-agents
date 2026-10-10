"""A80: Heal acts on why the server refused a food ``Take`` or a drink.

There is no try cap. Each refusal's code decides what Heal sends next
(``healing.refusal_action``): arm and retry, forget the supply, walk onto the
food, wait for a later tick, or hold the supply until the situation it was
refused in changes. Free-play run 5 wrote both potions off under the old cap
and died at 2/10 holding them.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.healing import REFUSAL_WAIT_TICKS, refusal_action
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import Entity, WorldModel, ZoneFact

KNIFE, POTION_A, POTION_B = InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion"), InventorySupply(5, "small_potion")
USE_SELF = {"verb": "Use", "target": {"kind": "self"}}
FOOD = Entity("supply", 8, (4, 3), "apple")


def hurt_world() -> WorldModel:
    """Map 7, all dirt, 7/10 health at (3, 3), a knife armed and two potions held."""
    w = WorldModel(character_id=9, map_id=7, pos=(3, 3), perception=6, health=7, max_health=10)
    for x in range(8):
        for y in range(8):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (3, 3), 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True), (3, 3): ZoneFact(safe=False)}
    w.armed_code = "pocket_knife"
    w.held_supplies = [KNIFE, POTION_A, POTION_B]
    return w


class RefusalTest(unittest.TestCase):
    def setUp(self):
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
        self.lines: list[str] = []
        self.r = Runner(cfg, None, 1, threading.Event(), out=self.lines.append, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(self.r.trace.close)
        self.r.world = hurt_world()
        self.r.mem = Memory(need_self=False, need_position=False)

    def decide(self) -> list[dict]:
        """One decision, sent the way the runner sends it."""
        r = self.r
        r.file_heal_refusals()  # as ``Runner.tick`` does before deciding
        d = r._decide(r.world, r.mem)
        intents = r.intents_for(d) or []
        r._forget_replaced_drink(intents)
        return [i for i in intents if i.get("verb") != "Wait"]

    def refuse(self, index: int, code: str, *, category: str = "", retryability: str = "") -> None:
        rejection = {"code": code, "category": category, "retryability": retryability}
        self.r.on_result({"outcome": "rejected", "rejection": rejection, "tick": self.r.world.tick}, index)
        self.r.file_heal_refusals()  # the next round trip files it before deciding

    def next_tick(self, n: int = 1) -> None:
        self.r.world.tick += n
        self.r.mem.heal_rearm = None  # the re-arm a drink leaves due is A24's, not under test here


class DrinkRefusalTest(RefusalTest):
    def test_nothing_armed_arms_and_retries(self):
        # armed_code says the potion is armed, so the drink is a Use alone;
        # the server says nothing is armed: the next drink sends its Arm.
        w = self.r.world
        w.armed_code, w.held_supplies = "small_potion", [POTION_A]
        self.assertEqual(self.decide(), [USE_SELF])
        self.refuse(0, "nothing_armed", category="missing", retryability="precondition")
        self.next_tick()
        self.assertEqual(self.decide(), [{"verb": "Arm", "supply_id": 4}, USE_SELF])

    def test_nothing_armed_after_its_own_arm_holds(self):
        # The Arm went out with the Use and it still found nothing armed:
        # arming again is nothing new, so that potion waits for a change.
        self.assertEqual(self.decide(), [{"verb": "Arm", "supply_id": 4}, USE_SELF])
        self.r.on_result({"outcome": "applied"}, 0)
        self.refuse(1, "nothing_armed", category="missing", retryability="precondition")
        self.next_tick()
        self.assertEqual(self.decide()[0], {"verb": "Arm", "supply_id": 5}, "the other potion is drunk")

    def test_not_held_on_the_arm_forgets_the_potion(self):
        self.decide()
        self.refuse(0, "not_held", category="missing", retryability="precondition")
        w = self.r.world
        w.health, w.pos = 6, (2, 2)  # whatever changes, potion 4 is not carried
        self.next_tick()
        self.assertEqual(self.decide()[0], {"verb": "Arm", "supply_id": 5})
        self.assertEqual(self.r.mem.heal_refusals[("use", 4)].action, "forget")

    def test_dead_or_on_cooldown_waits_for_a_later_tick(self):
        self.decide()
        self.refuse(0, "character_dead", category="state", retryability="transient")
        self.r.world.alive = False
        self.next_tick(REFUSAL_WAIT_TICKS)
        self.assertEqual(self.decide(), [], "downed: nothing to drink")
        self.r.world.alive = True
        self.assertEqual(self.decide()[0], {"verb": "Arm", "supply_id": 4}, "back up: the same potion")

    def test_a_persistent_transient_refusal_is_resent_once_a_cooldown_not_every_tick(self):
        w = self.r.world
        w.held_supplies = [KNIFE, POTION_A]
        for _ in range(3):
            self.decide()
            self.refuse(0, "store_unavailable", category="state", retryability="transient")
            for _ in range(REFUSAL_WAIT_TICKS - 1):
                self.next_tick()
                self.assertNotIn(USE_SELF, self.decide(), "inside the wait: no drink")
            self.next_tick()
            self.assertIn(USE_SELF, self.decide(), "the wait is over: drink again")

    def test_an_unknown_code_is_traced_and_held_until_something_changes(self):
        w = self.r.world
        w.held_supplies = [KNIFE, POTION_A]
        self.decide()
        self.refuse(0, "brand_new_code")
        self.assertTrue(any("unknown refusal brand_new_code" in line for line in self.lines))
        for _ in range(5):
            self.next_tick(7)
            self.assertNotIn(USE_SELF, self.decide(), "the same situation: not sent again")
        w.held_supplies = [KNIFE, POTION_A, InventorySupply(6, "apple")]  # a new item
        self.next_tick()
        self.assertEqual(self.decide()[0], {"verb": "Arm", "supply_id": 6}, "carried food first")
        w.held_supplies = [KNIFE, POTION_A]
        w.pos = (3, 4)  # it moved
        self.next_tick()
        self.assertEqual(self.decide()[0], {"verb": "Arm", "supply_id": 4})

    def test_health_moving_alone_does_not_resend_a_held_refusal(self):
        # Regen, poison or a hit changes health every few ticks; none of it is
        # what the refusal depends on, so it is not sent again.
        w = self.r.world
        w.held_supplies = [KNIFE, POTION_A]
        self.decide()
        self.refuse(0, "not_allowed_in_safe_zone", category="invalid", retryability="precondition")
        for health in (6, 7, 5, 8, 4):
            w.health = health
            self.next_tick()
            self.assertNotIn(USE_SELF, self.decide(), f"health {health}")

    def test_no_cap_however_many_refusals(self):
        # Free-play run 5 died holding two potions written off by a cap.
        w = self.r.world
        w.held_supplies = [KNIFE, POTION_A]
        for n in range(10):
            w.pos = (3, 3 + n % 2)  # the situation moves on each time
            self.next_tick()
            self.assertEqual(self.decide(), [{"verb": "Arm", "supply_id": 4}, USE_SELF], f"refusal {n}")
            self.refuse(0, "odd_code")

    def test_a_drink_is_always_arm_and_use_together(self):
        # Free-play run 3: only the Arm went out. A cooldown too long for the
        # queue sends neither, never the Arm alone.
        w, m = self.r.world, self.r.mem
        self.assertEqual(self.decide(), [{"verb": "Arm", "supply_id": 4}, USE_SELF])
        w.tick, m.last_use_tick = 100, 99
        d = self.r._decide(w, m)
        self.assertEqual([i["verb"] for i in d.submit_queue if i["verb"] != "Wait"], ["Arm", "Use"])

    def test_a_probe_files_no_refusal_and_keeps_those_filed(self):
        r = self.r
        r.world.held_supplies = [KNIFE, POTION_A]
        self.decide()
        self.refuse(0, "odd_code")
        r.file_heal_refusals()
        filed = dict(r.mem.heal_refusals)
        r.mem.held_queue = {"queue_id": "q", "next_index": 0}
        r.mem.pending_intents = [{"verb": "Step", "direction": "north"}] * 5
        r.world.health = 5
        for _ in range(3):
            r.reflex_while_held()
        self.assertEqual(r.mem.heal_refusals, filed)


class FoodRefusalTest(RefusalTest):
    def setUp(self):
        super().setUp()
        self.r.world.entities = [FOOD]
        self.r.world.held_supplies = [KNIFE]

    def test_supply_gone_forgets_the_food(self):
        self.assertEqual(self.decide(), [{"verb": "Take", "supply_id": 8}])
        self.refuse(0, "supply_gone", category="missing", retryability="permanent")
        self.r.world.health = 6
        self.next_tick()
        self.assertNotIn({"verb": "Take", "supply_id": 8}, self.decide())

    def test_out_of_reach_walks_onto_the_food_then_takes_it(self):
        w = self.r.world
        self.assertEqual(self.decide(), [{"verb": "Take", "supply_id": 8}])
        self.refuse(0, "target_not_nearby", category="range", retryability="precondition")
        self.next_tick()
        self.assertEqual(self.decide(), [{"verb": "Step", "direction": "right"}])
        self.r.mem.pending_intents, self.r.mem.held_queue = None, None
        w.pos = FOOD.pos
        self.next_tick()
        self.assertEqual(self.decide(), [{"verb": "Take", "supply_id": 8}])
        # Refused again on its own cell: walking is nothing new, so it holds.
        self.refuse(0, "target_not_nearby", category="range", retryability="precondition")
        self.next_tick()
        self.assertNotIn({"verb": "Take", "supply_id": 8}, self.decide())

    def test_food_that_moved_is_aimed_at_afresh(self):
        self.decide()
        self.refuse(0, "target_not_nearby", category="range", retryability="precondition")
        self.r.world.entities = [Entity("supply", 8, (3, 4), "apple")]
        self.next_tick()
        self.assertEqual(self.decide(), [{"verb": "Take", "supply_id": 8}])

    def test_an_applied_take_clears_the_refusal(self):
        self.decide()
        self.refuse(0, "odd_code")
        self.r.mem.pending = {"verb": "Take", "supply_id": 8}
        self.r.on_result({"outcome": "applied"}, 0)
        self.assertEqual(self.r.mem.heal_refusals, {})


class FakeClient:
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def tick(self, cid, intents, snapshot_version=None):
        self.sent.append(intents)
        return self.responses.pop(0)


class FiledAfterTheObservationTest(unittest.TestCase):
    """The situation is the one the next decision sees, not the world before
    the refused queue ran: an ``Arm`` that applied before its ``Use`` was
    refused leaves the potion armed, and the hold must be keyed on that."""

    def test_an_applied_arm_then_a_refused_use_is_not_resent(self):
        armed = {"gems": 0, "armed": {"id": 4, "supply_subtype_code": "small_potion"}, "worn": {}, "chest": [],
                 "held": [{"id": 4, "supply_subtype_code": "small_potion"}]}
        refused = {
            "tick": 12,
            "queue_id": "q1",
            "intent_results": [
                {"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"},
                {"queue_id": "q1", "index": 1, "tick": 12, "outcome": "rejected", "rejection": {"code": "odd_code"}},
            ],
            "events_by_tick": [],
            "observation": {"version": 2, "delta": {"inventory": armed}},
        }
        quiet = {"tick": 13, "intent_results": [], "events_by_tick": []}
        client = FakeClient([refused, quiet])
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = hurt_world()
        r.world.tick, r.world.armed_code, r.world.held_supplies = 10, None, [POTION_A]  # no weapon to put back
        r.mem = Memory(need_self=False, need_position=False)
        r.tick()
        self.assertEqual([i["verb"] for i in client.sent[0]], ["Arm", "Use"])
        self.assertEqual(r.world.armed_code, "small_potion")
        r.mem.held_queue = r.mem.pending_intents = r.mem.pending = None
        r.tick()
        self.assertNotIn(USE_SELF, client.sent[1] or [], "the same situation: the Use is not sent again")
        self.assertEqual(r.mem.heal_refusals[("use", 4)].situation[2], "small_potion")


class RefusalActionTest(unittest.TestCase):
    def action(self, code, verb="Use", category="", retryability="", **kw):
        kw.setdefault("armed_first", False)
        kw.setdefault("on_target", False)
        return refusal_action({"code": code, "category": category, "retryability": retryability}, verb=verb, **kw)

    def test_each_reason(self):
        self.assertEqual(self.action("nothing_armed"), "arm")
        self.assertEqual(self.action("not_held"), "arm")
        self.assertEqual(self.action("not_held", verb="Arm"), "forget")
        self.assertEqual(self.action("supply_gone", verb="Take"), "forget")
        self.assertEqual(self.action("target_not_nearby", verb="Take"), "walk")
        self.assertEqual(self.action("target_not_nearby", verb="Take", on_target=True), "hold")
        self.assertEqual(self.action("character_dead", category="state", retryability="transient"), "wait")
        self.assertEqual(self.action("conflict_lost", category="occupied", retryability="transient"), "wait")
        self.assertEqual(self.action("would_strand", verb="Arm", category="invalid", retryability="precondition"), "hold")
        self.assertEqual(self.action("not_transferable", retryability="permanent"), "forget")
        self.assertEqual(self.action("character_ended", category="state", retryability="permanent"), "forget")
        self.assertEqual(self.action("brand_new_code"), "hold")


if __name__ == "__main__":
    unittest.main()
