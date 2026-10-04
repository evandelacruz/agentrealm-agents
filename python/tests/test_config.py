"""Character files are checked on load, so a typo fails at start and not mid-run."""

import tempfile
import unittest
from pathlib import Path

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
                p.write_text(f'name = "A"\navatar = "default"\nmodel_agent = "m"\n[policy]\n{body}\n')
                with self.assertRaisesRegex(config.ConfigError, key):
                    config.load(p)


if __name__ == "__main__":
    unittest.main()
