"""Character selection and profile file keying (A59)."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import __main__ as cli, config
from agentrealm_agent.character_select import CharacterSelectionError, resolve_character_id
from agentrealm_agent.client import ApiError


class ResolveCharacterIdTest(unittest.TestCase):
    def cfg(self, world: str = "sandbox") -> config.CharacterConfig:
        return config.CharacterConfig("wren", world, config.Policy(), Path("wren.toml"))

    def test_by_id_flag(self):
        client = mock.Mock()
        self.assertEqual(
            resolve_character_id(client, self.cfg(), character_id=42, character_name=None),
            42,
        )
        client.list_characters.assert_not_called()

    def test_by_environment(self):
        client = mock.Mock()
        with mock.patch.dict("os.environ", {"AGENTREALM_CHARACTER_ID": "7"}):
            self.assertEqual(resolve_character_id(client, self.cfg(), character_id=None, character_name=None), 7)

    def test_id_flag_overrides_environment(self):
        env = {"AGENTREALM_CHARACTER_ID": "7"}
        self.assertEqual(
            resolve_character_id(mock.Mock(), self.cfg(), character_id=42, environ=env),
            42,
        )

    def test_name_flag_overrides_environment(self):
        client = mock.Mock()
        client.list_characters.return_value = [{"id": 5, "name": "Pat", "world_code": "sandbox"}]
        env = {"AGENTREALM_CHARACTER_ID": "7"}
        self.assertEqual(
            resolve_character_id(client, self.cfg(), character_name="Pat", environ=env),
            5,
        )

    def test_by_name_filters_world(self):
        client = mock.Mock()
        client.list_characters.return_value = [
            {"id": 1, "name": "Pat", "world_code": "sandbox"},
            {"id": 2, "name": "Pat", "world_code": "olympuff"},
        ]
        self.assertEqual(
            resolve_character_id(client, self.cfg("olympuff"), character_id=None, character_name="Pat"),
            2,
        )

    def test_ambiguous_name_lists_choices(self):
        client = mock.Mock()
        client.list_characters.return_value = [
            {"id": 1, "name": "Pat", "world_code": "sandbox"},
            {"id": 3, "name": "Pat", "world_code": "sandbox"},
        ]
        with self.assertRaisesRegex(CharacterSelectionError, "several characters"):
            resolve_character_id(client, self.cfg(), character_id=None, character_name="Pat")

    def test_missing_name(self):
        client = mock.Mock()
        client.list_characters.return_value = []
        with self.assertRaisesRegex(CharacterSelectionError, "no character named"):
            resolve_character_id(client, self.cfg(), character_id=None, character_name="Nobody")

    def test_id_and_name_together(self):
        with self.assertRaisesRegex(CharacterSelectionError, "not both"):
            resolve_character_id(mock.Mock(), self.cfg(), character_id=1, character_name="Pat")

    def test_neither_id_nor_name(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(CharacterSelectionError, "AGENTREALM_CHARACTER_ID"):
                resolve_character_id(mock.Mock(), self.cfg(), character_id=None, character_name=None)


class ProfileAndCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patch = mock.patch.object(config, "STATE_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        self.toml = self.dir / "wren.toml"
        self.toml.write_text('world = "sandbox"\n', encoding="utf-8")

    def main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        base_env = {"AGENTREALM_API_KEY": "k", "AGENTREALM_CHARACTER_ID": "42"}
        if env is not None:
            base_env.update(env)
        with mock.patch.dict("os.environ", base_env, clear=True), \
                mock.patch.object(cli, "Client") as client_cls, \
                mock.patch.object(cli, "Runner") as runner_cls:
            client = client_cls.return_value
            client.self_.return_value = {"lives": 1, "alive": True, "placed": True}
            client.create_character.return_value = {"id": 99}
            client.list_characters.return_value = [{"id": 5, "name": "Pat", "world_code": "sandbox"}]
            runner = runner_cls.return_value
            runner.run.side_effect = lambda: None
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue(), client

    def test_create_prints_id_only(self):
        rc, out, _, client = self.main("create", str(self.toml), "--name", "Pat")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "99")
        client.create_character.assert_called_once()
        self.assertFalse(any(p.suffix == ".json" for p in self.dir.iterdir()))

    def test_run_rejects_missing_character(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"AGENTREALM_API_KEY": "k", "AGENTREALM_CHARACTER_ID": "42"}, clear=True), \
                mock.patch.object(cli, "Client") as client_cls:
            client = client_cls.return_value
            client.self_.side_effect = ApiError(404, "character_not_found")
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = cli.main(["run", str(self.toml)])
        self.assertEqual(rc, 2)
        self.assertIn("character_not_found", err.getvalue())

    def test_trace_path_keyed_by_profile_and_id(self):
        cfg = config.load(self.toml)
        self.assertEqual(cfg.trace_path(42), self.dir / "wren.42.trace.jsonl")

    def test_metrics_with_character_id(self):
        trace = self.dir / "wren.42.trace.jsonl"
        trace.write_text(json.dumps({"call": "tick", "gems": 5}) + "\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = cli.main(["metrics", str(self.toml), "--character-id", "42"])
        self.assertEqual(rc, 0)
        self.assertIn('"gems": 5', out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_metrics_with_trace_path(self):
        trace = self.dir / "any.trace.jsonl"
        trace.write_text(json.dumps({"call": "tick", "gems": 2}) + "\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = cli.main(["metrics", str(trace)])
        self.assertEqual(rc, 0)
        self.assertIn('"gems": 2', out.getvalue())

    def test_metrics_rejects_character_name(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["metrics", str(self.toml), "--character-name", "Pat"])

    def test_run_name_flag_beats_environment_id(self):
        # main() exports AGENTREALM_CHARACTER_ID=42; the flag picks character 5.
        rc, _, err, client = self.main("run", str(self.toml), "--character-name", "Pat")
        self.assertEqual(rc, 0, err)
        client.self_.assert_called_with(5)

    def test_legacy_name_field_rejected(self):
        bad = self.dir / "bad.toml"
        bad.write_text('name = "x"\nworld = "sandbox"\n', encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load(bad)


if __name__ == "__main__":
    unittest.main()
