"""Runtime directives (A8): params, reload, never_attack."""

import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import (
    PARAM_DEFAULTS,
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

    def test_non_finite_keeps_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            for raw in ("inf", "-inf", "nan"):
                p.write_text(
                    f"params = {{ fight_margin = {raw}, curiosity = {raw}, risk = {raw}, retreat_hits = {raw} }}\n"
                )
                d = load_directives(p)
                self.assertEqual(d.params, PARAM_DEFAULTS, raw)

    def test_valid_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            p.write_text('params = { curiosity = 0.0, potion_reserve = 0 }\nnever_attack = ["character"]\n')
            d = load_directives(p)
            self.assertEqual(d.params["curiosity"], 0.0)
            self.assertEqual(d.params["potion_reserve"], 0)
            self.assertEqual(d.never_attack, ["character"])


class ReloadTest(unittest.TestCase):
    @staticmethod
    def _write(p: Path, text: str, mtime: float) -> None:
        p.write_text(text)
        os.utime(p, (mtime, mtime))

    def test_reload_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            watch = DirectivesWatch(p)
            self.assertFalse(watch.maybe_reload())
            self._write(p, 'never_attack = ["character"]\n', 1_000_000)
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["character"])
            self.assertFalse(watch.maybe_reload())
            self._write(p, 'never_attack = ["goblin"]\n', 1_000_010)
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["goblin"])

    def test_a_broken_file_keeps_the_last_good_directives(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            watch = DirectivesWatch(p)
            self._write(p, 'never_attack = ["character"]\n', 1_000_000)
            self.assertTrue(watch.maybe_reload())
            self._write(p, 'never_attack = ["character"\n', 1_000_010)
            with self.assertLogs("agentrealm_agent.directives", "WARNING"):
                self.assertFalse(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["character"])
            self.assertFalse(watch.maybe_reload())
            self._write(p, 'never_attack = ["goblin"]\n', 1_000_020)
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["goblin"])

    def test_a_fixed_file_reloads_when_its_mtime_collides(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            watch = DirectivesWatch(p)
            self._write(p, 'never_attack = ["character"]\n', 1_000_000)
            self.assertTrue(watch.maybe_reload())
            # Broken edit and its fix both land on the last good mtime.
            self._write(p, 'never_attack = ["character", "goblin"\n', 1_000_000)
            with self.assertLogs("agentrealm_agent.directives", "WARNING"):
                self.assertFalse(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["character"])
            self._write(p, 'never_attack = ["character", "goblin"]\n', 1_000_000)
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives.never_attack, ["character", "goblin"])

    def test_a_deleted_file_restores_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            watch = DirectivesWatch(p)
            self._write(p, 'never_attack = ["character"]\n', 1_000_000)
            self.assertTrue(watch.maybe_reload())
            p.unlink()
            self.assertTrue(watch.maybe_reload())
            self.assertEqual(watch.directives, default_directives())
            self.assertFalse(watch.maybe_reload())

    def test_a_broken_file_on_first_load_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wren.directives.toml"
            p.write_text("params = {\n")
            watch = DirectivesWatch(p)
            with self.assertLogs("agentrealm_agent.directives", "WARNING"):
                watch.ensure_loaded()
            self.assertEqual(watch.directives, default_directives())


class NeverAttackTest(unittest.TestCase):
    def test_blocks_character_fight_reflex(self):
        w = WorldModel(character_id=1, map_id=1, pos=(1, 1), perception=3)
        for x in range(3):
            for y in range(3):
                w.view.tiles[(x, y)] = "dirt"
        w.entities = [Entity("character", 5, (2, 1))]
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"], hostile_range=2)
        d = decide(w, Memory(), pol, random.Random(0), never_attack=["character"])
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

    def test_executor_drops_block_use_on_never_attack_npc(self):
        intent = {"verb": "Use", "target": {"kind": "block", "x": 2, "y": 1}}
        ents = [Entity("npc", 9, (2, 1), code="goblin")]
        self.assertTrue(use_blocked_by_never_attack(intent, ents, ["goblin"]))
        self.assertFalse(use_blocked_by_never_attack(intent, ents, ["character"]))

    def test_executor_drops_npc_use_on_never_attack_npc(self):
        intent = {"verb": "Use", "target": {"kind": "npc", "npc_id": 9}}
        ents = [Entity("npc", 9, (2, 1), code="goblin"), Entity("npc", 4, (3, 1), code="rat")]
        self.assertTrue(use_blocked_by_never_attack(intent, ents, ["goblin"]))
        self.assertFalse(use_blocked_by_never_attack(intent, ents, ["rat"]))
        self.assertFalse(use_blocked_by_never_attack(intent, ents, []))

    def test_executor_drops_npc_use_on_an_unknown_npc_id(self):
        # Fails closed: the server finds the NPC at run time, so an id missing
        # from the entity list may be a forbidden type (A45).
        intent = {"verb": "Use", "target": {"kind": "npc", "npc_id": 9}}
        self.assertTrue(use_blocked_by_never_attack(intent, [], ["goblin"]))
        self.assertTrue(use_blocked_by_never_attack(intent, [Entity("npc", 4, (3, 1), code="rat")], ["goblin"]))

    def test_executor_keeps_self_use(self):
        # Heal's Use on itself is eating or drinking, not an attack (A10).
        own = {"verb": "Use", "target": {"kind": "self"}}
        other = {"verb": "Use", "target": {"kind": "character", "character_id": 5}}
        ents = [Entity("character", 5, (0, 0))]
        self.assertFalse(use_blocked_by_never_attack(own, ents, ["character"]))
        self.assertTrue(use_blocked_by_never_attack(other, ents, ["character"]))


if __name__ == "__main__":
    unittest.main()
