"""A51: ExampleGreet teaching state and directives flag."""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.directives import Directives, load_directives
from agentrealm_agent.investigation import mark_npc_spoken
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.example_greet import ExampleGreetState
from agentrealm_agent.config import Policy
from agentrealm_agent.memory import Memory
from agentrealm_agent.world import Entity
from tests.test_states import ctx, world

dispatch_module = importlib.import_module("agentrealm_agent.states.dispatch")
from agentrealm_agent.states.explore import ExploreState


class ExampleGreetFlagTest(unittest.TestCase):
    def test_loads_flags_from_directives_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wren.directives.toml"
            path.write_text("[flags]\nexample_greet = true\n", encoding="utf-8")
            d = load_directives(path)
        self.assertTrue(d.flags.get("example_greet"))


class ExampleGreetStateTest(unittest.TestCase):
    def test_guard_false_without_flag(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 4, (2, 1))]
        state = ExampleGreetState()
        self.assertFalse(state.guard(w, ctx(w)))

    def test_act_says_hello(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 4, (2, 1), code="guard")]
        kb = KnowledgeBase.empty("sandbox")
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", goals=["explore"], on_hostile="ignore"),
            random.Random(0),
            knowledge=kb,
            directives=Directives(flags={"example_greet": True}),
        )
        out = ExampleGreetState().act(w, c)
        self.assertEqual(out.intents, [{"verb": "Say", "npc_id": 4, "text": "hello"}])

    def test_guard_false_after_npc_marked_spoken(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 4, (2, 1))]
        kb = KnowledgeBase.empty("sandbox")
        mark_npc_spoken(kb, 4)
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", on_hostile="ignore"),
            random.Random(0),
            knowledge=kb,
            directives=Directives(flags={"example_greet": True}),
        )
        self.assertFalse(ExampleGreetState().guard(w, c))


class ExampleGreetDispatchTest(unittest.TestCase):
    def test_runs_before_explore_when_enabled(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 4, (2, 1))]
        kb = KnowledgeBase.empty("sandbox")
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", goals=["explore"], on_hostile="ignore"),
            random.Random(0),
            knowledge=kb,
            directives=Directives(flags={"example_greet": True}),
        )
        mini = (ExampleGreetState(), ExploreState())
        with mock.patch.object(dispatch_module, "STATES", mini):
            out = dispatch(w, c)
        self.assertEqual(out.state, "ExampleGreet")
        self.assertEqual(out.intents[0]["verb"], "Say")

    def test_flag_off_skips_example_greet(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 4, (2, 1))]
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", goals=["explore"], on_hostile="ignore"),
            random.Random(0),
            directives=Directives(),
        )
        mini = (ExampleGreetState(), ExploreState())
        with mock.patch.object(dispatch_module, "STATES", mini):
            out = dispatch(w, c)
        self.assertEqual(out.state, "Explore")

    def test_act_falls_through_reason_when_npc_vanishes(self):
        w = world(["...", "...", "..."], at=(1, 1))
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", on_hostile="ignore"),
            random.Random(0),
            directives=Directives(flags={"example_greet": True}),
        )
        out = ExampleGreetState().act(w, c)
        self.assertEqual((out.intents, out.reason), (None, "no npc to greet"))
