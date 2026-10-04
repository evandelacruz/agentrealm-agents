"""Character files are checked on load, so a typo fails at start and not mid-run."""

import tempfile
import unittest
from pathlib import Path

from saims_agent import config


class ConfigTest(unittest.TestCase):
    def test_list_and_point_fields_are_type_checked(self):
        # A bare string for a list field would iterate its characters.
        cases = [
            ('hostile = "npc"', "policy.hostile"),
            ('goals = "explore"', "policy.goals"),
            ('avoid_blocks = "lava"', "policy.avoid_blocks"),
            ('goals = ["goto"]\ngoto = "2,3"', "policy.goto"),
        ]
        for body, key in cases:
            with self.subTest(key), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "c.toml"
                p.write_text(f'name = "A"\navatar = "default"\nmodel_agent = "m"\n[policy]\n{body}\n')
                with self.assertRaisesRegex(config.ConfigError, key):
                    config.load(p)


if __name__ == "__main__":
    unittest.main()
