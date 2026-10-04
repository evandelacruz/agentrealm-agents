import importlib.util
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
