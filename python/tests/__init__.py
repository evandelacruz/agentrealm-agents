"""Unit tests (``make test``).

No test reaches the network: the Supplies reference (A54) is never fetched
and no cached copy is read, so every run started in a test plays from the
copy checked in with the agent.

No test reads or writes ``python/.state`` (A84): before the agent is imported,
``AGENTREALM_STATE_DIR`` points at a fresh temp dir, so traces and the world
knowledge base land there. ``load_tests`` adds ``StateDirUntouched`` after
every other test; it fails if anything under ``python/.state`` changed.

This is the one place those switches live; ``make test`` discovers the tests
as this package so it always runs first.
"""

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REAL_STATE_DIR = Path(__file__).resolve().parent.parent / ".state"
TEST_STATE_DIR = Path(tempfile.mkdtemp(prefix="agentrealm-test-state-"))
atexit.register(shutil.rmtree, TEST_STATE_DIR, ignore_errors=True)
os.environ["AGENTREALM_STATE_DIR"] = str(TEST_STATE_DIR)


def state_snapshot(root: Path = REAL_STATE_DIR) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` for every file under ``root``."""
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


REAL_STATE_BEFORE = state_snapshot()

from agentrealm_agent import config, supplies  # noqa: E402  (after the env override above)

REAL_FETCH = supplies.fetch  # for the test of fetch itself
mock.patch.object(supplies, "fetch", lambda *args, **kwargs: None).start()
mock.patch.object(supplies, "CACHE_PATH", supplies.BUNDLED_PATH.with_name("no-cache-in-tests.json")).start()


class StateDirUntouched(unittest.TestCase):
    """Runs last in ``make test``: the suite left ``python/.state`` as it found it."""

    def test_state_dir_is_the_temp_dir(self):
        self.assertEqual(config.STATE_DIR, TEST_STATE_DIR.resolve())

    def test_real_state_dir_unchanged(self):
        self.assertEqual(state_snapshot(), REAL_STATE_BEFORE, f"tests changed {REAL_STATE_DIR}")


def load_tests(loader, standard_tests, pattern):
    """Discover the package as usual, then append ``StateDirUntouched`` so it runs after everything else.

    A package's ``load_tests`` must find the submodules itself (``standard_tests``
    holds only this file's tests), hence the inner ``discover``. ``top_level_dir``
    is ``python/``, so modules load as ``tests.test_*`` as in ``make test``.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    suite = loader.discover(start_dir=here, pattern=pattern or "test*.py", top_level_dir=os.path.dirname(here))
    suite.addTests(loader.loadTestsFromTestCase(StateDirUntouched))
    return suite
