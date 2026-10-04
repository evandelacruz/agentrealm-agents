import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "seed_local_stack",
    _ROOT / "scripts" / "seed_local_stack.py",
)
assert _SPEC and _SPEC.loader
seed = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = seed
_SPEC.loader.exec_module(seed)


class TestExtractVerifyToken(unittest.TestCase):
    def test_from_verify_link(self):
        log = "INFO signup link http://localhost:8080/accounts/verify-email?token=abc123XYZ-_"
        self.assertEqual(seed.extract_verify_token(log), "abc123XYZ-_")

    def test_from_json_snippet(self):
        log = 'mail skipped; use POST /accounts/me/first-api-key with {"token":"tok_9AbCdEfGh"}'
        self.assertEqual(seed.extract_verify_token(log), "tok_9AbCdEfGh")

    def test_missing(self):
        self.assertIsNone(seed.extract_verify_token("no token here"))

    def test_prefers_newest(self):
        log = (
            "link http://x/accounts/verify-email?token=old111\n"
            "link http://x/accounts/verify-email?token=new222\n"
        )
        self.assertEqual(seed.extract_verify_token(log), "new222")

    def test_prefers_line_naming_email(self):
        log = (
            "to a@x.test http://x/accounts/verify-email?token=mine\n"
            "to b@x.test http://x/accounts/verify-email?token=theirs\n"
        )
        self.assertEqual(seed.extract_verify_token(log, "a@x.test"), "mine")


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(obj):
    return _Resp(obj if isinstance(obj, bytes) else json.dumps(obj).encode())


def _err(status, code):
    return urllib.error.HTTPError(
        "http://x", status, "err", {}, io.BytesIO(json.dumps({"code": code}).encode())
    )


class FakeServer:
    """Answers urlopen from a list of (method, path) -> response or exception."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        key = (req.get_method(), "/" + path)
        self.calls.append(key)
        answers = self.routes[key]
        out = answers.pop(0) if isinstance(answers, list) else answers
        if isinstance(out, Exception):
            raise out
        return out


class TestHttp(unittest.TestCase):
    def test_http_error_code(self):
        srv = FakeServer({("POST", "/accounts"): _err(409, "email_taken")})
        with mock.patch.object(seed.urllib.request, "urlopen", srv):
            with self.assertRaises(seed.ApiError) as cm:
                seed.Http("http://x").request("POST", "/accounts", {"email": "a"})
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "email_taken"))

    def test_network_error_is_status_zero(self):
        srv = FakeServer({("GET", "/accounts/me"): urllib.error.URLError("refused")})
        with mock.patch.object(seed.urllib.request, "urlopen", srv):
            with self.assertRaises(seed.ApiError) as cm:
                seed.Http("http://x").request("GET", "/accounts/me")
        self.assertEqual(cm.exception.status, 0)


class TestProbe(unittest.TestCase):
    PATH = ("POST", "/worlds/sandbox/characters")

    def run_probe(self, routes, timeout=5.0):
        srv = FakeServer(routes)
        with mock.patch.object(seed.urllib.request, "urlopen", srv), mock.patch.object(
            seed.time, "sleep"
        ):
            seed.wait_for_sandbox_create(seed.Http("http://x"), "k", timeout)
        return srv

    def test_retries_until_ready(self):
        srv = self.run_probe({self.PATH: [_err(409, "world_not_ready"), _ok({"id": 1})]})
        self.assertEqual(srv.calls.count(self.PATH), 2)

    def test_name_taken_ok_only_when_probe_exists(self):
        listing = [{"name": seed.PROBE_NAME, "world_code": "sandbox"}]
        self.run_probe({self.PATH: _err(409, "name_taken"), ("GET", "/characters"): _ok(listing)})

    def test_name_taken_without_probe_raises(self):
        with self.assertRaises(seed.ApiError):
            self.run_probe({self.PATH: _err(409, "name_taken"), ("GET", "/characters"): _ok([])})

    def test_identity_reuse_raises(self):
        with self.assertRaises(seed.ApiError):
            self.run_probe({self.PATH: _err(409, "identity_reuse")})

    def test_times_out(self):
        with self.assertRaises(SystemExit):
            self.run_probe({self.PATH: _err(409, "world_not_ready")}, timeout=0.0)


class TestEnvFile(unittest.TestCase):
    def test_export_lines_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "local.env")
            seed.write_env_file(path, "http://x", "sekrit", "a@x.test")
            text = Path(path).read_text()
            self.assertIn("export AGENTREALM_BASE_URL=http://x\n", text)
            self.assertIn("export AGENTREALM_API_KEY=sekrit\n", text)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(seed.read_env_file(path)["AGENTREALM_API_KEY"], "sekrit")


class TestMain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.dir.name, "local.env")
        self.addCleanup(self.dir.cleanup)

    def run_main(self, routes, *argv, logs="", stdout=None):
        srv = FakeServer(routes)
        proc = mock.Mock(returncode=0, stdout=logs, stderr="")
        with mock.patch.object(seed.urllib.request, "urlopen", srv), mock.patch.object(
            seed.subprocess, "run", return_value=proc
        ) as run, mock.patch.object(seed.time, "sleep"), mock.patch("sys.stdout", stdout or io.StringIO()), mock.patch(
            "sys.stderr", io.StringIO()
        ):
            rc = seed.main(["--base-url", "http://x", "--write-env", self.env, *argv])
        return rc, srv, run

    def test_fresh_signup_scrapes_logs_and_mints(self):
        logs = "to dev-local@agentrealm.test http://x/accounts/verify-email?token=tok1\n"
        routes = {
            ("GET", "/healthz"): _ok(b"ok"),
            ("POST", "/accounts"): _ok({}),
            ("POST", "/accounts/me/first-api-key"): _ok({"secret": "key1"}),
        }
        out = io.StringIO()
        rc, srv, run = self.run_main(routes, "--stack-dir", self.dir.name, logs=logs, stdout=out)
        self.assertEqual(rc, 0)
        self.assertIn("export AGENTREALM_API_KEY=key1", Path(self.env).read_text())
        self.assertEqual(out.getvalue(), "")  # the key is not printed unless asked
        self.assertNotIn(("POST", "/worlds/sandbox/characters"), srv.calls)  # probe is opt-in
        since = [a for a in run.call_args.args[0] if a.startswith("--since=")]
        self.assertRegex(since[0], r"--since=\d{4}-\d\d-\d\dT")

    def test_rerun_reuses_saved_key(self):
        seed.write_env_file(self.env, "http://x", "key1", "dev-local@agentrealm.test")
        routes = {
            ("GET", "/healthz"): _ok(b"ok"),
            ("POST", "/accounts"): _err(409, "email_taken"),
            ("GET", "/accounts/me"): _ok({}),
        }
        rc, srv, run = self.run_main(routes)
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertNotIn(("POST", "/accounts/me/first-api-key"), srv.calls)

    def test_rerun_without_saved_key_exits(self):
        routes = {("GET", "/healthz"): _ok(b"ok"), ("POST", "/accounts"): _err(409, "email_taken")}
        with self.assertRaises(SystemExit) as cm:
            self.run_main(routes)
        self.assertIn("already exists", str(cm.exception))

    def test_network_failure_after_health_is_a_clean_exit(self):
        routes = {
            ("GET", "/healthz"): _ok(b"ok"),
            ("POST", "/accounts"): urllib.error.URLError("connection refused"),
        }
        with self.assertRaises(SystemExit) as cm:
            self.run_main(routes)
        self.assertIn("network error", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
