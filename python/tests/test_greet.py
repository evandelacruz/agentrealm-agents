"""Talking to NPCs (A64): the planner sees them in State, and Greet says
hello once to a nearby one not yet spoken to."""

import json
import random
import unittest

from agentrealm_agent.clues import note_spoken_clue
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.executor.pacing import SPEECH_INTERVAL_TICKS
from agentrealm_agent.investigation import MAX_REJECTIONS, mark_npc_spoken
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.greet import GREET_RETRY_TICKS
from agentrealm_agent.strategist import NEARBY_NPCS_SHOWN, build_prompt
from agentrealm_agent.world import Entity, WorldModel


def field(width: int = 15, height: int = 9, at=(2, 4), perception: int = 8) -> WorldModel:
    """Open dirt, fully known."""
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception, tick=100)
    for x in range(width):
        for y in range(height):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 7
    w.health, w.max_health = 10, 10
    return w


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
    def state(self, w: WorldModel, kb: KnowledgeBase | None) -> str:
        messages = build_prompt(
            triggers=[],
            w=w,
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=kb,
        )
        return messages[1]["content"]

    def test_nearby_npcs_with_spoken_and_hostile(self):
        w = field()
        w.entities = [
            Entity("npc", 4, (5, 4), "fake_helper"),
            Entity("npc", 5, (2, 1), "fake_biter"),
            Entity("npc", 6, (14, 4), "fake_far"),  # out of sight (perception 8)
            Entity("supply", 7, (3, 4), "apple"),
        ]
        w.threat.record(("npc", "fake_biter"), 2)  # that type has hit us
        kb = KnowledgeBase.empty("sandbox")
        mark_npc_spoken(kb, 4)
        mark_npc_spoken(kb, 99)
        content = self.state(w, kb)
        self.assertIn("npcs_spoken_to=2", content)
        line = next(l for l in content.splitlines() if l.startswith("nearby_npcs="))
        rows = json.loads(line.split("=", 1)[1])
        self.assertEqual(
            rows,
            [
                {"id": 4, "type": "fake_helper", "cells": 3, "dir": "east", "spoken": True, "hostile": False},
                {"id": 5, "type": "fake_biter", "cells": 3, "dir": "north", "spoken": False, "hostile": True},
            ],
        )

    def test_only_the_nearest_are_listed(self):
        w = field()
        w.entities = [Entity("npc", 10 + i, (3 + i, 4), "fake_helper") for i in range(8)]
        line = next(l for l in self.state(w, None).splitlines() if l.startswith("nearby_npcs="))
        rows = json.loads(line.split("=", 1)[1])
        self.assertEqual([r["id"] for r in rows], [10 + i for i in range(NEARBY_NPCS_SHOWN)])

    def test_a_helper_reply_reaches_the_planner_as_a_clue(self):
        w = field()
        w.entities = [Entity("npc", 4, (5, 4), "fake_helper")]
        kb, m = KnowledgeBase.empty("sandbox"), Memory()
        note_spoken_clue(kb, m, w, {"kind": "SpokenTo", "speaker_kind": "npc", "speaker_id": 4, "text": "Seek the old well."}, 100)
        self.assertEqual(m.clue_signals[-1]["trigger"], "clue")
        clues = self.state(w, kb).split("Clues (oldest first):\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(json.loads(clues)[0]["text"], "Seek the old well.")

    def test_the_op_contract_documents_say(self):
        content = build_prompt(
            triggers=[], w=field(), plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=None,
        )[0]["content"]
        self.assertIn("- say: text, and exactly one of npc_id", content)
        self.assertIn("npc_type", content)


class GreetTest(unittest.TestCase):
    def test_greets_a_nearby_npc_once(self):
        w = field()
        w.entities = [Entity("npc", 4, (5, 4), "fake_helper")]
        kb, m = KnowledgeBase.empty("sandbox"), Memory()
        out = dispatch(w, ctx(travel(12, 4), kb, m))
        self.assertEqual(out.state, "Greet")
        self.assertEqual(out.intents, [{"verb": "Say", "npc_id": 4, "text": "hello"}])
        # The runner records the applied Say; the NPC is never greeted again.
        mark_npc_spoken(kb, 4)
        m.last_speech_tick = w.tick
        for _ in range(5):
            w.tick += GREET_RETRY_TICKS
            self.assertEqual(says(dispatch(w, ctx(travel(12, 4), kb, m))), [])

    def test_the_nearest_unspoken_npc_first(self):
        w = field()
        w.entities = [Entity("npc", 4, (8, 4), "fake_helper"), Entity("npc", 5, (5, 4), "fake_helper")]
        kb = KnowledgeBase.empty("sandbox")
        self.assertEqual(says(dispatch(w, ctx(None, kb)))[0]["npc_id"], 5)
        mark_npc_spoken(kb, 5)
        self.assertEqual(says(dispatch(w, ctx(None, kb)))[0]["npc_id"], 4)

    def test_retries_are_bounded(self):
        # The Say never lands (no result recorded): retried after a pause, at most MAX_REJECTIONS times.
        w = field()
        w.entities = [Entity("npc", 4, (5, 4), "fake_helper")]
        m = Memory()
        sent = 0
        for _ in range(40):
            sent += len(says(dispatch(w, ctx(None, None, m))))
            w.tick += 5
        self.assertEqual(sent, MAX_REJECTIONS)

    def test_waits_out_the_speech_cooldown(self):
        w = field()
        w.entities = [Entity("npc", 4, (5, 4), "fake_helper")]
        m = Memory(last_speech_tick=w.tick - 1)
        self.assertEqual(says(dispatch(w, ctx(None, None, m))), [])
        w.tick += SPEECH_INTERVAL_TICKS
        self.assertEqual(len(says(dispatch(w, ctx(None, None, m)))), 1)

    def test_never_walks_to_an_npc_out_of_sight(self):
        w = field(width=30, perception=4)
        w.entities = [Entity("npc", 4, (12, 4), "fake_helper")]  # 10 cells: inside Say's reach, out of sight
        out = dispatch(w, ctx(None))
        self.assertNotEqual(out.state, "Greet")
        self.assertEqual(says(out), [])

    def test_a_say_op_is_left_to_investigate(self):
        w = field()
        w.entities = [Entity("npc", 4, (5, 4), "fake_helper")]
        p = Plan([{"op": "say", "npc_id": 4, "text": "any news?"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(p))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(says(out)[0]["text"], "any news?")


class NoGreetUnderThreatTest(unittest.TestCase):
    def test_not_with_a_known_hostile_near(self):
        w = field()
        w.entities = [Entity("npc", 4, (8, 4), "fake_helper"), Entity("npc", 5, (2, 1), "fake_biter")]
        w.threat.record(("npc", "fake_biter"), 2)
        out = dispatch(w, ctx(travel(12, 4), hostile=["character"]))
        self.assertEqual(says(out), [])

    def test_not_after_a_recent_hit(self):
        w = field()
        w.entities = [Entity("npc", 4, (8, 4), "fake_helper")]
        w.attacked_tick = w.tick - 5
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4), hostile=["character"]))), [])

    def test_not_a_hostile_npc(self):
        w = field()
        w.entities = [Entity("npc", 5, (8, 4), "fake_biter")]
        w.threat.record(("npc", "fake_biter"), 2)
        self.assertEqual(says(dispatch(w, ctx(travel(12, 4), hostile=["character"]))), [])

    def test_not_off_safe_ground_with_a_policy_hostile_in_range(self):
        w = field()
        w.entities = [Entity("npc", 4, (3, 4), "fake_helper")]
        out = dispatch(w, ctx(None, on_hostile="ignore", hostile=["npc"], hostile_range=2))
        self.assertEqual(says(out), [])


class WalkResumesTest(unittest.TestCase):
    def test_travel_walks_on_after_the_hello(self):
        w = field()
        w.entities = [Entity("npc", 4, (5, 1), "fake_helper")]
        kb, m, p = KnowledgeBase.empty("sandbox"), Memory(), travel(12, 4)
        outs = []
        for _ in range(4):
            out = dispatch(w, ctx(p, kb, m))
            outs.append(out)
            for i in out.intents or []:
                if i["verb"] == "SetPosition":
                    w.pos = (i["x"], i["y"])
                elif i["verb"] == "Say":
                    mark_npc_spoken(kb, i["npc_id"])  # as the runner does on an applied Say
                    m.last_speech_tick = w.tick
            w.tick += 4
        self.assertEqual([o.state for o in outs], ["Greet", "Travel", "Travel", "Travel"])
        self.assertIsNotNone(p.current(), "the hello did not cost the travel op")
        self.assertGreater(w.pos[0], 2)


if __name__ == "__main__":
    unittest.main()
