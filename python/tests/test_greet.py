"""Talking to NPCs (A64): the planner sees them in State, and Greet says
hello once to a nearby likely helper not yet spoken to."""

import json
import random
import threading
import unittest
from pathlib import Path

from agentrealm_agent.clues import note_spoken_clue
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.executor.pacing import SPEECH_INTERVAL_TICKS
from agentrealm_agent.investigation import (
    HELPER_STILL_TICKS,
    MAX_REJECTIONS,
    greeted_npc_ids,
    mark_npc_greeted,
    mark_npc_spoken,
    say_key,
    spoken_npc_ids,
)
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.greet import GREET_RETRY_TICKS
from agentrealm_agent.strategist import NEARBY_NPCS_SHOWN, build_prompt
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def field(width: int = 15, height: int = 9, at=(2, 4), perception: int = 8, safe: bool = True) -> WorldModel:
    """Open dirt, fully known; safe ground (a town) unless ``safe`` is False."""
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception, tick=100)
    for x in range(width):
        for y in range(height):
            w.view.tiles[(x, y)] = "dirt"
            if safe:
                apply_zone(w, 7, x, y, {"safe": True})
    w.terrain_center, w.terrain_map = at, 7
    w.health, w.max_health = 10, 10
    return w


def place(w: WorldModel, *entities: Entity, still: int = HELPER_STILL_TICKS) -> None:
    """Put ``entities`` in view, each NPC standing on its cell for ``still`` ticks."""
    w.entities = list(entities)
    w.npc_still = {e.id: (e.pos, w.tick - still) for e in entities if e.kind == "npc"}


def helper(npc_id: int, pos) -> Entity:
    return Entity("npc", npc_id, pos, "fake_helper")


def travel(x: int, y: int) -> Plan:
    return Plan([{"op": "travel", "to": "point", "x": x, "y": y}], dict(PARAM_DEFAULTS))


def ctx(plan: Plan | None = None, kb: KnowledgeBase | None = None, m: Memory | None = None, **policy_kw) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", goals=[], **policy_kw),
        random.Random(0),
        knowledge=kb if kb is not None else KnowledgeBase.empty("sandbox"),
        plan=plan,
    )


def says(out) -> list[dict]:
    return [i for i in out.intents or [] if i["verb"] == "Say"]


class StateListsNpcsTest(unittest.TestCase):
    def prompt(self, w: WorldModel, kb: KnowledgeBase | None) -> list[dict]:
        return build_prompt(
            triggers=[],
            w=w,
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=kb,
        )

    def rows(self, w: WorldModel, kb: KnowledgeBase | None = None) -> list[dict]:
        line = next(l for l in self.prompt(w, kb)[1]["content"].splitlines() if l.startswith("nearby_npcs="))
        return json.loads(line.split("=", 1)[1])

    def test_nearby_npcs_with_spoken_hostile_and_stays_put(self):
        w = field()
        place(
            w,
            helper(4, (5, 4)),
            Entity("npc", 5, (2, 1), "fake_biter"),
            Entity("npc", 6, (14, 4), "fake_far"),  # out of sight (perception 8)
            Entity("supply", 7, (3, 4), "apple"),
        )
        w.npc_still[5] = ((2, 1), w.tick)  # just moved there
        w.threat.record(("npc", "fake_biter"), 2)  # that type has hit us
        kb = KnowledgeBase.empty("sandbox")
        mark_npc_spoken(kb, 4)
        mark_npc_spoken(kb, 99)
        mark_npc_greeted(kb, 5)
        self.assertIn("npcs_spoken_to=2 npcs_greeted=1", self.prompt(w, kb)[1]["content"])
        self.assertEqual(
            self.rows(w, kb),
            [
                {"id": 4, "type": "fake_helper", "cells": 3, "dir": "east",
                 "spoken": True, "greeted": False, "hostile": False, "stays_put": True},
                {"id": 5, "type": "fake_biter", "cells": 3, "dir": "north",
                 "spoken": False, "greeted": True, "hostile": True, "stays_put": False},
            ],
        )

    def test_only_the_nearest_are_listed(self):
        w = field()
        place(w, *[helper(10 + i, (3 + i, 4)) for i in range(8)])
        self.assertEqual([r["id"] for r in self.rows(w)], [10 + i for i in range(NEARBY_NPCS_SHOWN)])

    def test_a_helper_reply_reaches_the_planner_as_a_clue(self):
        w = field()
        place(w, helper(4, (5, 4)))
        kb, m = KnowledgeBase.empty("sandbox"), Memory()
        event = {"kind": "SpokenTo", "speaker_kind": "npc", "speaker_id": 4, "text": "Seek the old well."}
        note_spoken_clue(kb, m, w, event, 100)
        self.assertEqual(m.clue_signals[-1]["trigger"], "clue")
        content = self.prompt(w, kb)[1]["content"]
        clues = content.split("Clues (oldest first):\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(json.loads(clues)[0]["text"], "Seek the old well.")

    def test_the_op_contract_documents_say(self):
        system = self.prompt(field(), None)[0]["content"]
        self.assertIn("- say: text, and exactly one of npc_id", system)
        self.assertIn("npc_type", system)
        self.assertIn("does not say which NPCs are helpers", system)


class WorldNotesStillNpcsTest(unittest.TestCase):
    def test_an_npc_that_moves_starts_again(self):
        w = field()
        w.apply_entities({"tick": 100, "npcs": [{"id": 4, "x": 5, "y": 4, "npc_type_code": "fake_helper"}]})
        w.tick = 160
        w.apply_entities({"tick": 160, "npcs": [{"id": 4, "x": 5, "y": 4, "npc_type_code": "fake_helper"}]})
        self.assertEqual(w.npc_still_ticks(w.entities[0]), 60)
        w.tick = 170
        w.apply_entities({"tick": 170, "npcs": [{"id": 4, "x": 6, "y": 4, "npc_type_code": "fake_helper"}]})
        self.assertEqual(w.npc_still_ticks(w.entities[0]), 0)
        w.apply_entities({"tick": 170, "npcs": []})
        self.assertEqual(w.npc_still, {}, "only NPCs in view are kept")


class GreetTest(unittest.TestCase):
    def test_greets_a_nearby_helper_once(self):
        w = field()
        place(w, helper(4, (5, 4)))
        kb, m = KnowledgeBase.empty("sandbox"), Memory()
        out = dispatch(w, ctx(travel(12, 4), kb, m))
        self.assertEqual(out.state, "Greet")
        self.assertEqual(out.intents, [{"verb": "Say", "npc_id": 4, "text": "hello"}])
        # The runner records the applied hello; the NPC is never greeted again.
        mark_npc_greeted(kb, 4)
        m.last_speech_tick = w.tick
        for _ in range(5):
            w.tick += GREET_RETRY_TICKS
            self.assertEqual(says(dispatch(w, ctx(travel(12, 4), kb, m))), [])

    def test_the_nearest_unspoken_npc_first(self):
        w = field()
        place(w, helper(4, (8, 4)), helper(5, (5, 4)))
        kb = KnowledgeBase.empty("sandbox")
        self.assertEqual(says(dispatch(w, ctx(None, kb)))[0]["npc_id"], 5)
        mark_npc_greeted(kb, 5)
        self.assertEqual(says(dispatch(w, ctx(None, kb)))[0]["npc_id"], 4)
        mark_npc_spoken(kb, 4)  # a say op's text counts too: nothing left to greet
        self.assertEqual(says(dispatch(w, ctx(None, kb))), [])

    def test_retries_are_bounded(self):
        # The Say never lands (no result recorded): retried after a pause, at most MAX_REJECTIONS times.
        w = field()
        place(w, helper(4, (5, 4)))
        m = Memory()
        sent = 0
        for _ in range(40):
            sent += len(says(dispatch(w, ctx(None, None, m))))
            w.tick += 5
        self.assertEqual(sent, MAX_REJECTIONS)

    def test_waits_out_the_speech_cooldown(self):
        w = field()
        place(w, helper(4, (5, 4)))
        m = Memory(last_speech_tick=w.tick - 1)
        self.assertEqual(says(dispatch(w, ctx(None, None, m))), [])
        w.tick += SPEECH_INTERVAL_TICKS
        self.assertEqual(len(says(dispatch(w, ctx(None, None, m)))), 1)

    def test_never_walks_to_an_npc_out_of_sight(self):
        w = field(width=30, perception=4)
        place(w, helper(4, (12, 4)))  # 10 cells: inside Say's reach, out of sight
        out = dispatch(w, ctx(None))
        self.assertNotEqual(out.state, "Greet")
        self.assertEqual(says(out), [])

    def test_a_say_op_is_left_to_investigate(self):
        w = field()
        place(w, helper(4, (5, 4)))
        p = Plan([{"op": "say", "npc_id": 4, "text": "any news?"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(p))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(says(out)[0]["text"], "any news?")


class NotAMonsterTest(unittest.TestCase):
    """The API names no helpers: a monster that has not hit us yet is not greeted."""

    def test_not_an_npc_that_moves(self):
        w = field()
        place(w, Entity("npc", 4, (8, 4), "fake_roamer"), still=HELPER_STILL_TICKS - 1)
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4)))), [])

    def test_not_off_safe_ground_at_any_range(self):
        # Unmeasured, standing still, well past hostile_range: still not greeted in the field.
        w = field(safe=False)
        place(w, Entity("npc", 4, (9, 4), "fake_idler"))
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4), hostile=["npc"], hostile_range=2))), [])

    def test_off_safe_ground_when_the_policy_does_not_fear_npcs(self):
        w = field(safe=False)
        place(w, helper(4, (9, 4)))
        self.assertEqual(len(says(dispatch(w, ctx(travel(12, 4), hostile=["character"])))), 1)

    def test_not_a_known_hostile_npc(self):
        w = field()
        place(w, Entity("npc", 5, (8, 4), "fake_biter"))
        w.threat.record(("npc", "fake_biter"), 2)
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4)))), [])


class NoGreetUnderThreatTest(unittest.TestCase):
    def test_not_with_a_known_hostile_near(self):
        w = field()
        place(w, helper(4, (8, 4)), Entity("npc", 5, (2, 1), "fake_biter"))
        w.threat.record(("npc", "fake_biter"), 2)
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4)))), [])

    def test_not_after_a_recent_hit(self):
        w = field()
        place(w, helper(4, (8, 4)))
        w.attacked_tick = w.tick - 5
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4)))), [])


class WalkResumesTest(unittest.TestCase):
    def test_travel_walks_on_after_the_hello(self):
        w = field()
        place(w, helper(4, (5, 1)))
        kb, m, p = KnowledgeBase.empty("sandbox"), Memory(), travel(12, 4)
        outs = []
        for _ in range(4):
            out = dispatch(w, ctx(p, kb, m))
            outs.append(out)
            for i in out.intents or []:
                if i["verb"] == "SetPosition":
                    w.pos = (i["x"], i["y"])
                elif i["verb"] == "Say":
                    mark_npc_greeted(kb, i["npc_id"])  # as the runner does on an applied hello
                    m.last_speech_tick = w.tick
            w.tick += 4
        self.assertEqual([o.state for o in outs], ["Greet", "Travel", "Travel", "Travel"])
        self.assertIsNotNone(p.current(), "the hello did not cost the travel op")
        self.assertGreater(w.pos[0], 2)


class GreetIsNotASayOpTest(unittest.TestCase):
    """Greet keeps its own record: a hello never settles a planner ``say`` op."""

    def runner(self) -> Runner:
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[], pickup=False), Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = field()
        place(r.world, helper(4, (5, 4)))
        r.mem = Memory(need_self=False, need_position=False)
        return r

    def greet(self, r: Runner, outcome: dict) -> None:
        out = dispatch(r.world, ctx(None, r.knowledge, r.mem))
        self.assertEqual(out.state, "Greet")
        r.mem.pending = out.intents[0]
        r.on_result({"tick": r.world.tick, **outcome}, 0)

    def test_an_applied_hello_is_greeted_not_spoken(self):
        r = self.runner()
        self.greet(r, {"outcome": "applied"})
        self.assertEqual(greeted_npc_ids(r.knowledge), {4})
        self.assertEqual(spoken_npc_ids(r.knowledge), set())

    def test_a_say_op_still_says_its_text_after_the_hello(self):
        r = self.runner()
        self.greet(r, {"outcome": "applied"})
        r.world.tick += 50
        r.mem.last_speech_tick = None
        p = Plan([{"op": "say", "npc_id": 4, "text": "any news?"}], dict(PARAM_DEFAULTS))
        out = dispatch(r.world, ctx(p, r.knowledge, r.mem))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(says(out), [{"verb": "Say", "npc_id": 4, "text": "any news?"}])
        r.mem.pending = out.intents[0]
        r.on_result({"tick": r.world.tick, "outcome": "applied"}, 0)
        self.assertEqual(spoken_npc_ids(r.knowledge), {4})

    def test_a_say_op_of_hello_is_settled_by_the_hello(self):
        r = self.runner()
        self.greet(r, {"outcome": "applied"})
        p = Plan([{"op": "say", "npc_id": 4, "text": "hello"}], dict(PARAM_DEFAULTS))
        out = dispatch(r.world, ctx(p, r.knowledge, r.mem))
        self.assertEqual(says(out), [], "the same words were already said")
        self.assertIsNone(p.current())

    def test_a_refused_hello_spends_no_say_op_budget(self):
        r = self.runner()
        self.greet(r, {"outcome": "rejected", "rejection": {"category": "target", "code": "target_out_of_range"}})
        self.assertNotIn(say_key(4), r.mem.investigate_rejections)
        self.assertEqual(greeted_npc_ids(r.knowledge), set())


class HeldQueueTest(unittest.TestCase):
    """While a walk queue is held, the runner's reflex probe runs the dispatcher
    every round trip; Greet is not a reflex there and must not spend its tries."""

    def runner(self) -> Runner:
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[], pickup=False), Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = field()
        place(r.world, helper(4, (5, 4)))
        r.mem = Memory(need_self=False, need_position=False)
        return r

    def test_the_probe_leaves_greetings_alone(self):
        r = self.runner()
        for _ in range(MAX_REJECTIONS + 2):
            self.assertIsNone(r.reflex_while_held(), "Greet does not drop a held queue")
            r.world.tick += GREET_RETRY_TICKS
        self.assertEqual(r.mem.greetings, {})
        d = r._decide(r.world, r.mem, plan=r.plan)
        self.assertEqual(d.intent, {"verb": "Say", "npc_id": 4, "text": "hello"}, "greeted at the next decision")


if __name__ == "__main__":
    unittest.main()
