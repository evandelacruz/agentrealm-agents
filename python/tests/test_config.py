"""Character files are checked on load, so a typo fails at start and not mid-run."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config


class ConfigTest(unittest.TestCase):
    def test_fields_are_type_checked(self):
        # A bare string for a list field would iterate its characters.
        cases = [
            ('hostile = "npc"', "policy.hostile"),
            ('goals = "explore"', "policy.goals"),
            ('avoid_blocks = "lava"', "policy.avoid_blocks"),
            ('goals = ["goto"]\ngoto = "2,3"', "policy.goto"),
            ('entity_refresh = "5"', "policy.entity_refresh"),
            ('hostile_range = 1.5', "policy.hostile_range"),
            ('pickup = "yes"', "policy.pickup"),
            ('seed = "abc"', "policy.seed"),
            ('goto = [1, 2]\ngoto_map = "2"', "policy.goto_map"),
        ]
        for body, key in cases:
            with self.subTest(key), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "c.toml"
                p.write_text(f'world = "sandbox"\n[policy]\n{body}\n')
                with self.assertRaisesRegex(config.ConfigError, key):
                    config.load(p)


if __name__ == "__main__":
    unittest.main()


class StateDirTest(unittest.TestCase):
    """``AGENTREALM_STATE_DIR`` moves traces and the knowledge base (A84)."""

    def test_env_overrides_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {config.STATE_DIR_ENV: tmp}):
            self.assertEqual(config.state_dir(), Path(tmp).resolve())

    def test_default_is_python_dot_state(self):
        env = {k: v for k, v in os.environ.items() if k != config.STATE_DIR_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.state_dir(), config.DEFAULT_STATE_DIR)
        self.assertEqual(config.DEFAULT_STATE_DIR, Path(config.__file__).resolve().parent.parent / ".state")
