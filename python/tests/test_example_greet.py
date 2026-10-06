"""A51: ExampleGreet teaching state, kept out of the shipped dispatcher."""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.example_greet import ExampleGreetState
from agentrealm_agent.world import Entity
from tests.test_states import world

dispatch_module = importlib.import_module("agentrealm_agent.states.dispatch")


def ctx(knowledge=None, plan=None) -> PlayContext:
    return PlayContext(
        Memory(),
        Policy(kind="scripted", goals=["explore"], on_hostile="ignore"),
        random.Random(0),
        knowledge=knowledge,
        directives=default_directives(),
        plan=plan,
    )


def with_example(example: ExampleGreetState) -> tuple:
    """The shipped STATES with the example one slot above Explore, as the guide adds it."""
    states = list(dispatch_module.STATES)
    at = next(i for i, s in enumerate(states) if s.name == "Explore")
    return tuple(states[:at] + [example] + states[at:])


class ShippedDispatcherTest(unittest.TestCase):
    def test_example_is_not_in_shipped_states(self):
        self.assertNotIn("ExampleGreet", [s.name for s in dispatch_module.STATES])

    def test_shipped_agent_does_not_greet_characters(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1))]
        out = dispatch(w, ctx())
        self.assertNotEqual(out.state, "ExampleGreet")
        self.assertFalse(any(i.get("character_id") == 9 for i in out.intents or []))


class ExampleGreetDispatchTest(unittest.TestCase):
    def test_greets_each_character_once_without_knowledge_base(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1))]
        c = ctx()
        with mock.patch.object(dispatch_module, "STATES", with_example(ExampleGreetState())):
            first = dispatch(w, c)
            second = dispatch(w, c)
        self.assertEqual(first.state, "ExampleGreet")
        self.assertEqual(first.intents, [{"verb": "Say", "character_id": 9, "text": "hello"}])
        self.assertEqual(second.state, "Explore")

    def test_nearest_first_then_the_next(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        w.entities = [Entity("character", 5, (3, 1)), Entity("character", 6, (2, 1))]
        c = ctx()
        with mock.patch.object(dispatch_module, "STATES", with_example(ExampleGreetState())):
            ids = [dispatch(w, c).intents[0].get("character_id") for _ in range(3)]
        self.assertEqual(ids[:2], [6, 5])
        self.assertIsNone(ids[2])  # Explore's step, not a third hello

    def test_investigate_still_carries_out_a_say_op_first(self):
        w = world([".....", ".....", "....."], at=(0, 1))
        w.entities = [Entity("npc", 4, (3, 1)), Entity("character", 9, (1, 1))]
        plan = Plan([{"op": "say", "npc_id": 4, "text": "hello"}], dict(PARAM_DEFAULTS))
        c = ctx(KnowledgeBase.empty("sandbox"), plan=plan)
        with mock.patch.object(dispatch_module, "STATES", with_example(ExampleGreetState())):
            out = dispatch(w, c)
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents, [{"verb": "Say", "npc_id": 4, "text": "hello"}])

    def test_memory_is_per_character(self):
        example = ExampleGreetState()
        a = world(["...", "...", "..."], at=(1, 1))
        b = world(["...", "...", "..."], at=(1, 1))
        b.character_id = 2
        a.entities = b.entities = [Entity("character", 9, (2, 1))]
        example.act(a, ctx())
        self.assertFalse(example.guard(a, ctx()))
        self.assertTrue(example.guard(b, ctx()))


class ExampleGreetGuardTest(unittest.TestCase):
    def test_guard_false_when_out_of_sight(self):
        w = world(["." * 12], at=(0, 0))
        w.entities = [Entity("character", 9, (10, 0))]
        self.assertFalse(ExampleGreetState().guard(w, ctx()))

    def test_guard_false_when_downed(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1))]
        w.alive = False
        self.assertFalse(ExampleGreetState().guard(w, ctx()))

    def test_guard_ignores_npcs(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 4, (2, 1))]
        self.assertFalse(ExampleGreetState().guard(w, ctx()))

    def test_act_with_no_one_falls_through(self):
        w = world(["...", "...", "..."], at=(1, 1))
        out = ExampleGreetState().act(w, ctx())
        self.assertEqual((out.intents, out.reason, out.wait), (None, "no one to greet", False))
