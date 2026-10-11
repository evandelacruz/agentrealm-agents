"""A98: every API call spends from the character's call budget, whoever sends it."""

import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.client import ApiError, Client
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.pacer import BURST, Pacer, Pacers

CID = 7
HZ = 10


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class BucketServer:
    """The server's limiter, written from manual §7.4 and not from the pacer:
    one token per tick, burst 3, one a second asleep. Past it is 429."""

    def __init__(self, clock: FakeClock, *, asleep=False, refuse_first: float | None = None):
        self.clock, self.asleep = clock, asleep
        self.refuse_first = refuse_first  # answer the first call 429 with this Retry-After
        self.tokens, self.seen = float(BURST), int(clock.now * HZ)
        self.sent: list[tuple[float, str]] = []
        self.refused = 0

    def send(self, method, path, body=None, query=None):
        now = self.clock.now
        index = int(now * HZ)
        per_window = (1 / HZ) / 1.0 if self.asleep else 1.0
        self.tokens = min(BURST, self.tokens + (index - self.seen) * per_window)
        self.seen = index
        if not path.startswith(f"/characters/{CID}/"):
            return {}
        self.sent.append((now, path))
        if self.refuse_first is not None:
            retry, self.refuse_first = self.refuse_first, None
            self.tokens = 0.0
            raise ApiError(429, "rate_limited", retry)
        if self.tokens < 1 - 1e-9:
            self.refused += 1
            raise ApiError(429, "rate_limited")
        self.tokens -= 1
        if path.endswith("/world"):
            return {"tick_rate_hz": HZ}
        if path.endswith("/self"):
            return {"alive": True, "asleep": self.asleep} if self.asleep else {"alive": True}
        return {"tick": index}


def client_on(server: BucketServer, clock: FakeClock) -> Client:
    c = Client("http://test", "key", pacers=Pacers(clock=clock.time, sleep=clock.sleep))
    c._send = server.send
    return c


class PacerTest(unittest.TestCase):
    def test_a_burst_of_three_then_one_per_window(self):
        clock = FakeClock(1000.0)
        p = Pacer(window=0.1, clock=clock.time, sleep=clock.sleep)
        for _ in range(BURST):
            p.spend()
        self.assertEqual(clock.now, 1000.0, "the burst costs no wait")
        p.spend()
        self.assertGreaterEqual(clock.now, 1000.1)

    def test_asleep_refills_once_a_second(self):
        clock = FakeClock(1000.0)
        p = Pacer(window=0.1, asleep=True, tokens=0.0, clock=clock.time, sleep=clock.sleep)
        p.spend()
        self.assertGreaterEqual(clock.now, 1001.0)

    def test_a_429_empties_it_and_holds_for_retry_after(self):
        clock = FakeClock(1000.0)
        p = Pacer(window=0.1, clock=clock.time, sleep=clock.sleep)
        p.drain(2.0)
        p.spend()
        self.assertGreaterEqual(clock.now, 1002.0)


class ClientPacingTest(unittest.TestCase):
    def test_held_queue_polls_sent_back_to_back_stay_in_budget(self):
        # A Use, then "queue held" polls with nothing between them: the
        # caller never waits, so only the client can keep them in budget.
        clock = FakeClock()
        server = BucketServer(clock)
        c = client_on(server, clock)
        c.world(CID)
        c.tick(CID, [{"verb": "Use", "target": {"kind": "self"}}])
        for _ in range(8):
            c.tick(CID)
        self.assertEqual(server.refused, 0)
        self.assertEqual(len(server.sent), 10)

    def test_asleep_is_held_to_one_a_second(self):
        clock = FakeClock()
        server = BucketServer(clock, asleep=True)
        c = client_on(server, clock)
        c.self_(CID)
        for _ in range(5):
            c.tick(CID, [])
        self.assertEqual(server.refused, 0)

    def test_after_a_429_it_waits_out_retry_after(self):
        clock = FakeClock()
        server = BucketServer(clock, refuse_first=2.0)
        c = client_on(server, clock)
        with self.assertRaises(ApiError):
            c.tick(CID)
        refused_at = clock.now
        c.tick(CID)
        self.assertGreaterEqual(server.sent[-1][0] - refused_at, 2.0)
        self.assertEqual(server.refused, 0)

    def test_every_character_route_spends_a_token(self):
        # No method can reach /characters/{id}/… without the pacer.
        clock = FakeClock()
        c = Client("http://test", "key", pacers=Pacers(clock=clock.time, sleep=clock.sleep))
        c._send = lambda *args, **kwargs: {}
        pacer = c.pacers.get(CID)
        for name, method in inspect.getmembers(c, inspect.ismethod):
            params = inspect.signature(method).parameters
            if name.startswith("_") or next(iter(params), None) != "cid":
                continue
            required = [p for p in params.values() if p.default is p.empty and p.kind is p.POSITIONAL_OR_KEYWORD]
            with self.subTest(name):
                pacer.tokens, pacer.hold_until = float(BURST), 0.0
                method(CID, *([1] * (len(required) - 1)))
                self.assertEqual(pacer.tokens, BURST - 1)
        c.list_characters()  # an account route: not the character's budget
        self.assertEqual(pacer.tokens, BURST - 1)


class RunnerSharesTheBudgetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_runner_waits_on_the_pacer_its_client_spends_from(self):
        clock = FakeClock()
        c = client_on(BucketServer(clock), clock)
        cfg = CharacterConfig("T", "sandbox", Policy(goals=[]), Path("t.toml"))
        r = runner.Runner(cfg, c, CID, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        self.assertIs(r.pacer, c.pacers.get(CID))


if __name__ == "__main__":
    unittest.main()
