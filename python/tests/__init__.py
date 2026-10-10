"""Unit tests (``make test``).

No test reaches the network: the Supplies reference (A54) is never fetched
and no cached copy is read, so every run started in a test plays from the
copy checked in with the agent. This is the one place that switch lives;
``make test`` discovers the tests as this package so it always runs first.
"""

from unittest import mock

from agentrealm_agent import supplies

mock.patch.object(supplies, "fetch", lambda *args, **kwargs: None).start()
mock.patch.object(supplies, "CACHE_PATH", supplies.BUNDLED_PATH.with_name("no-cache-in-tests.json")).start()
