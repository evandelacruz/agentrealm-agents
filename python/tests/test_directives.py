"""Runtime directives (A8): params, reload, never_attack."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import (
    DirectivesWatch,
    attack_forbidden,
    default_directives,
    load_directives,
    use_blocked_by_never_attack,
)
from agentrealm_agent.world import Entity, WorldModel


class ParamTest(unittest.TestCase):
    def test_defaults(self):
        d = default_directives()
        self.assertEqual(d.params["fight_margin"], 1.5)
        self.assertEqual(d.params["retreat_hits"], 2)

    def test_out_of_range_keeps_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            p.write_text('params = { fight_margin = 0.5, risk = 2.0, retreat_hits = 0 }\n')
            d = load_directives(p)
            self.assertEqual(d.params["fight_margin"], 1.5)
            self.assertEqual(d.params["risk"], 0.5)
            self.assertEqual(d.params["retreat_hits"], 2)

    def test_valid_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            p.write_text('params = { curiosity = 0.0, potion_reserve = 0 }\nnever_attack = ["character"]\n')
            d = load_directives(p)
            self.assertEqual(d.params["curiosity"], 0.0)
            self.assertEqual(d.params["potion_reserve"], 0)
            self.assertEqual(d.never_attack, ["character"])


class ReloadTest(unittest.TestCase):
    def test_reload_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            watch = DirectivesWatch(p)
            self.assertFalse(watch.maybe_reload())
            p.write_text('never_attack = ["character"]\n')
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["character"])
            time.sleep(0.02)
            p.write_text('never_attack = ["goblin"]\n')
            os.utime(p, None)
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["goblin"])


class NeverAttackTest(unittest.TestCase):
    def test_blocks_character_fight_reflex(self):
        w = WorldModel(character_id=1, map_id=1, pos=(1, 1), perception=3)
        for x in range(3):
            for y in range(3):
                w.view.tiles[(x, y)] = "dirt"
        w.entities = [Entity("character", 5, (2, 1))]
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"], hostile_range=2)
        d = decide(w, Memory(), pol, __import__("random").Random(0), never_attack=["character"])
        self.assertIsNotNone(d.intent)
        self.assertEqual(d.intent["verb"], "SetPosition", d.reason)

    def test_attack_forbidden_npc_type(self):
        e = Entity("npc", 1, (0, 0), code="goblin")
        self.assertTrue(attack_forbidden(e, ["goblin"]))
        self.assertFalse(attack_forbidden(e, ["character"]))

    def test_executor_drops_use(self):
        intent = {"verb": "Use", "target": {"kind": "character", "character_id": 5}}
        ents = [Entity("character", 5, (0, 0))]
        self.assertTrue(use_blocked_by_never_attack(intent, ents, ["character"]))


if __name__ == "__main__":
    unittest.main()
