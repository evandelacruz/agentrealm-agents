"""Unit tests (``make test``).

No test reaches the network: the Supplies reference (A54) is never fetched
and no cached copy is read, so every run started in a test plays from the
copy checked in with the agent.

No test reads or writes ``python/.state`` (A84): before the agent is imported,
``AGENTREALM_STATE_DIR`` points at a fresh temp dir, so traces and the world
knowledge base land there. An audit hook records every file operation this
process makes under ``python/.state``, and ``load_tests`` adds
``StateDirUntouched`` after every other test to fail on any. It sees only this
process, so a ``run`` writing there at the same time cannot fail the suite.

This is the one place those switches live; ``make test`` discovers the tests
as this package so it always runs first.
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REAL_STATE_DIR = Path(__file__).resolve().parent.parent / ".state"  # config.DEFAULT_STATE_DIR, before import
TEST_STATE_DIR = Path(tempfile.mkdtemp(prefix="agentrealm-test-state-"))
atexit.register(shutil.rmtree, TEST_STATE_DIR, ignore_errors=True)
os.environ["AGENTREALM_STATE_DIR"] = str(TEST_STATE_DIR)

# Audit events that name a path as their first arguments (``os.rename`` names two).
_FILE_EVENTS = frozenset({"open", "os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.listdir", "os.scandir",
                          "os.truncate", "os.utime", "os.chmod", "os.chown", "os.link", "os.symlink"})
_REAL_PREFIX = os.path.join(str(REAL_STATE_DIR), "")
STATE_TOUCHES: list[str] = []  # "<event> <path>" for each file operation under python/.state


def _watch_real_state(event, args):
    if event not in _FILE_EVENTS:
        return
    for arg in args[:2]:
        if isinstance(arg, (str, bytes, os.PathLike)):
            path = os.path.abspath(os.fsdecode(arg))
            if path == str(REAL_STATE_DIR) or path.startswith(_REAL_PREFIX):
                STATE_TOUCHES.append(f"{event} {path}")


sys.addaudithook(_watch_real_state)

from agentrealm_agent import config, knowledge_base, supplies  # noqa: E402  (after the env override above)

REAL_FETCH = supplies.fetch  # for the test of fetch itself
mock.patch.object(supplies, "fetch", lambda *args, **kwargs: None).start()
mock.patch.object(supplies, "CACHE_PATH", supplies.BUNDLED_PATH.with_name("no-cache-in-tests.json")).start()


class StateDirUntouched(unittest.TestCase):
    """Runs last in ``make test``: no state path points into ``python/.state``, and no test opened anything there."""

    def test_state_paths_are_in_the_temp_dir(self):
        self.assertEqual(REAL_STATE_DIR, config.DEFAULT_STATE_DIR)
        self.assertEqual(config.STATE_DIR, TEST_STATE_DIR.resolve())
        for path in (knowledge_base.WORLDS_DIR, supplies.CACHE_PATH):
            self.assertFalse(path.resolve().is_relative_to(REAL_STATE_DIR), path)

    def test_no_test_touched_real_state_dir(self):
        self.assertEqual(STATE_TOUCHES, [], f"tests touched {REAL_STATE_DIR}")


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
