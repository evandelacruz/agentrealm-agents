"""Lorraine's command line (A138): create, run and status on Gary's profile files."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from ladder.__main__ import DEFAULT_CREATE_MODEL, main, run, status
from tests.test_starter_agent import FakeClient

PROFILE = "characters/starter.toml"


class LadderCommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        self.out = io.StringIO()

    def quiet(self, fn, *args):
        with redirect_stdout(self.out), redirect_stderr(self.out):
            return fn(*args)

    def main_with(self, client, argv):
        with mock.patch("ladder.__main__.Client", return_value=client):
            return self.quiet(main, ["--api-key", "k", *argv])

    def test_create_prints_the_new_id_with_the_scripted_model(self):
        client = mock.Mock()
        client.create_character.return_value = {"id": 7}
        self.assertEqual(self.main_with(client, ["create", PROFILE, "--name", "Pat"]), 0)
        self.assertEqual(self.out.getvalue().strip(), "7")
        self.assertEqual(client.create_character.call_args.args[1:], ("Pat", mock.ANY, DEFAULT_CREATE_MODEL))

    def test_create_failure_exits_nonzero(self):
        self.assertEqual(self.main_with(FakeClient(create_error=ApiError(400, "bad")), ["create", PROFILE]), 1)

    def test_status_prints_self(self):
        self.assertEqual(self.main_with(FakeClient(), ["status", PROFILE, "--character-id", "42"]), 0)
        self.assertIn("(42): lives=1 alive=True placed=True", self.out.getvalue())

    def test_status_of_an_unknown_character_exits_nonzero(self):
        client = FakeClient()
        client.self_ = mock.Mock(side_effect=ApiError(404, "character_not_found"))
        self.assertEqual(self.quiet(status, client, self.cfg, 42), 1)

    def test_run_plays_stans_loop(self):
        with mock.patch("starter_agent.run", return_value=0) as stan:
            self.assertEqual(self.main_with(FakeClient(), ["run", PROFILE, "--character-id", "42"]), 0)
        self.assertEqual(stan.call_args.args[2], 42)

    def test_run_rejects_missing_character(self):
        client = FakeClient()
        client.self_ = mock.Mock(side_effect=ApiError(404, "character_not_found"))
        self.assertEqual(self.quiet(run, client, self.cfg, 42), 2)

    def test_run_exits_nonzero_when_the_character_stops_on_error(self):
        self.assertEqual(self.quiet(run, FakeClient(world_error=ApiError(401, "unauthorized")), self.cfg, 42), 1)

    def test_run_needs_a_character(self):
        with mock.patch.dict("os.environ", {"AGENTREALM_CHARACTER_ID": ""}):
            self.assertEqual(self.main_with(FakeClient(), ["run", PROFILE]), 2)

    def test_main_needs_api_key_and_readable_profile(self):
        with mock.patch.dict("os.environ", {"AGENTREALM_API_KEY": ""}):
            self.assertEqual(self.quiet(main, ["run", PROFILE]), 2)
            self.assertEqual(self.quiet(main, ["--api-key", "k", "status", "missing.toml"]), 2)


if __name__ == "__main__":
    unittest.main()
