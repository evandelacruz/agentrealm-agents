"""Executor queue limits and intent shapes (M6), no server."""

import unittest

from agentrealm_agent.executor import (
    QUEUE_HORIZON_INTENTS,
    horizon_for_world,
    queue_horizon_intents,
    step,
    trim_to_horizon,
    wait,
    within_horizon,
)


class HorizonTest(unittest.TestCase):
    def test_default_horizon_is_forty_at_ten_hz(self):
        self.assertEqual(QUEUE_HORIZON_INTENTS, 40)
        self.assertEqual(queue_horizon_intents(), 40)
        self.assertEqual(
            horizon_for_world(tick_rate_hz=10, horizon_seconds=4),
            40,
        )

    def test_horizon_scales_with_world_clock(self):
        self.assertEqual(queue_horizon_intents(tick_rate_hz=20, horizon_seconds=4), 80)


class TrimTest(unittest.TestCase):
    def test_short_queue_unchanged(self):
        q = [wait(), step("right")]
        self.assertEqual(trim_to_horizon(q), q)
        self.assertTrue(within_horizon(q))

    def test_long_queue_keeps_prefix(self):
        q = [wait()] * 50
        trimmed = trim_to_horizon(q)
        self.assertEqual(len(trimmed), 40)
        self.assertEqual(trimmed, q[:40])
        self.assertFalse(within_horizon(q))

    def test_custom_limit(self):
        q = [step("up")] * 5
        self.assertEqual(trim_to_horizon(q, limit=3), q[:3])

    def test_trim_returns_copy(self):
        q = [wait()]
        trimmed = trim_to_horizon(q)
        self.assertEqual(trimmed, q)
        self.assertIsNot(trimmed, q)


class IntentShapeTest(unittest.TestCase):
    def test_step_and_wait_match_manual(self):
        self.assertEqual(step("up_left"), {"verb": "Step", "direction": "up_left"})
        self.assertEqual(wait(), {"verb": "Wait"})


if __name__ == "__main__":
    unittest.main()
