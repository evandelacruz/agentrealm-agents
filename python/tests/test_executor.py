"""Executor queue limits and intent shapes (M6), no server."""

import unittest

from agentrealm_agent.client import _tick_body
from agentrealm_agent.executor import (
    QUEUE_HORIZON_INTENTS,
    queue_horizon_intents,
    step,
    trim_to_horizon,
    wait,
    within_horizon,
)
from agentrealm_agent.executor.pacing import pace_steps, step_direction, ticks_per_move

H = QUEUE_HORIZON_INTENTS


class HorizonTest(unittest.TestCase):
    def test_default_horizon_is_forty_at_ten_hz(self):
        self.assertEqual(QUEUE_HORIZON_INTENTS, 40)
        self.assertEqual(queue_horizon_intents(), 40)
        self.assertEqual(queue_horizon_intents(tick_rate_hz=10, horizon_seconds=4), 40)

    def test_horizon_scales_with_world_clock(self):
        self.assertEqual(queue_horizon_intents(tick_rate_hz=20, horizon_seconds=4), 80)

    def test_rejects_non_positive_or_non_int_inputs(self):
        for bad in (0, -1, 2.5, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    queue_horizon_intents(tick_rate_hz=bad)
                with self.assertRaises(ValueError):
                    queue_horizon_intents(horizon_seconds=bad)


class TrimTest(unittest.TestCase):
    def test_short_queue_unchanged(self):
        q = [wait(), step("right")]
        self.assertEqual(trim_to_horizon(q, limit=H), q)
        self.assertTrue(within_horizon(q, limit=H))

    def test_exactly_at_horizon_fits(self):
        q = [wait()] * 40
        self.assertTrue(within_horizon(q, limit=H))
        self.assertEqual(trim_to_horizon(q, limit=H), q)

    def test_one_over_horizon_is_cut(self):
        q = [step("up")] * 40 + [wait()]
        self.assertFalse(within_horizon(q, limit=H))
        self.assertEqual(trim_to_horizon(q, limit=H), q[:40])

    def test_long_queue_keeps_prefix(self):
        q = [step("up"), step("down")] * 25
        self.assertEqual(trim_to_horizon(q, limit=H), q[:40])

    def test_zero_limit(self):
        self.assertEqual(trim_to_horizon([wait()], limit=0), [])
        self.assertTrue(within_horizon([], limit=0))
        self.assertFalse(within_horizon([wait()], limit=0))

    def test_rejects_negative_or_non_int_limit(self):
        for bad in (-1, 1.5, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    trim_to_horizon([wait()], limit=bad)
                with self.assertRaises(ValueError):
                    within_horizon([wait()], limit=bad)

    def test_trim_returns_copy(self):
        q = [wait()]
        trimmed = trim_to_horizon(q, limit=H)
        self.assertEqual(trimmed, q)
        self.assertIsNot(trimmed, q)


class IntentShapeTest(unittest.TestCase):
    def test_step_and_wait_match_tick_schema(self):
        self.assertEqual(step("up_left"), {"verb": "Step", "direction": "up_left"})
        self.assertEqual(wait(), {"verb": "Wait"})

    def test_queue_goes_through_client_tick_body(self):
        q = [step("right"), wait(), wait(), wait(), step("right")]
        self.assertEqual(_tick_body(q, None), {"intents": q})


class PacingTest(unittest.TestCase):
    def test_ticks_per_move_default_pace(self):
        self.assertEqual(ticks_per_move(10, 2500), 4)

    def test_pace_path_inserts_waits(self):
        intents, queued = pace_steps((0, 0), [(1, 0), (2, 0)], movement_speed=2500, tick_hz=10, horizon_ticks=40)
        self.assertEqual(queued, [(1, 0), (2, 0)])
        self.assertEqual(intents, [step("right"), wait(), wait(), wait(), step("right")])

    def test_horizon_caps_steps_and_drops_trailing_waits(self):
        intents, queued = pace_steps(
            (0, 0), [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)], movement_speed=2500, tick_hz=10, horizon_ticks=8
        )
        self.assertEqual(queued, [(1, 0), (2, 0)])
        self.assertEqual(intents, [step("right"), wait(), wait(), wait(), step("right")])

    def test_step_direction_diagonal(self):
        self.assertEqual(step_direction((1, 1), (2, 0)), "up_right")


if __name__ == "__main__":
    unittest.main()
