import unittest

from agentrealm_agent.executor.movement import (
    build_paced_walk_queue,
    direction_between,
    ticks_per_step,
)

STEP_RIGHT = {"verb": "Step", "direction": "right"}
STEP_DOWN = {"verb": "Step", "direction": "down"}
WAIT = {"verb": "Wait"}


class TicksPerStepTests(unittest.TestCase):
    def test_base_speed(self):
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed_milli=2500), 4)

    def test_faster(self):
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed_milli=5000), 2)
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed_milli=10000), 1)

    def test_uneven_speed_rounds_up(self):
        # 10000 / 3000 = 3.33 ticks: three would draw movement_cooldown.
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed_milli=3000), 4)
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed_milli=2400), 5)

    def test_faster_than_tick_rate_is_one(self):
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed_milli=20000), 1)

    def test_rejects_non_positive(self):
        for kwargs in (
            {"tick_rate_hz": 0, "movement_speed_milli": 2500},
            {"tick_rate_hz": -10, "movement_speed_milli": 2500},
            {"tick_rate_hz": 10, "movement_speed_milli": 0},
            {"tick_rate_hz": 10, "movement_speed_milli": -2500},
        ):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                ticks_per_step(**kwargs)


class DirectionBetweenTests(unittest.TestCase):
    def test_all_eight_directions(self):
        expected = {
            (2, 1): "up",
            (2, 3): "down",
            (1, 2): "left",
            (3, 2): "right",
            (1, 1): "up_left",
            (3, 1): "up_right",
            (1, 3): "down_left",
            (3, 3): "down_right",
        }
        for to, direction in expected.items():
            with self.subTest(to=to):
                self.assertEqual(direction_between((2, 2), to), direction)

    def test_rejects_zero_length_and_non_adjacent(self):
        for to in ((2, 2), (4, 2), (2, 0), (4, 4), (0, 3)):
            with self.subTest(to=to), self.assertRaises(ValueError):
                direction_between((2, 2), to)


class PacedWalkQueueTests(unittest.TestCase):
    def test_two_steps_at_base_speed(self):
        q = build_paced_walk_queue((0, 0), [(1, 0), (2, 0)], movement_speed_milli=2500)
        self.assertEqual(q, [STEP_RIGHT, WAIT, WAIT, WAIT, STEP_RIGHT])

    def test_five_blocks_per_second(self):
        q = build_paced_walk_queue((0, 0), [(0, 1), (0, 2)], movement_speed_milli=5000)
        self.assertEqual(q, [STEP_DOWN, WAIT, STEP_DOWN])

    def test_uneven_speed(self):
        q = build_paced_walk_queue((0, 0), [(1, 0), (2, 0)], movement_speed_milli=3000)
        self.assertEqual(q, [STEP_RIGHT, WAIT, WAIT, WAIT, STEP_RIGHT])

    def test_max_speed_no_waits(self):
        q = build_paced_walk_queue(
            (5, 5), [(6, 5), (7, 5), (7, 6)], movement_speed_milli=10000
        )
        self.assertEqual(q, [STEP_RIGHT, STEP_RIGHT, STEP_DOWN])

    def test_steps_are_one_period_apart(self):
        q = build_paced_walk_queue(
            (0, 0), [(i, 0) for i in range(1, 6)], movement_speed_milli=2500
        )
        step_ticks = [i for i, intent in enumerate(q) if intent["verb"] == "Step"]
        self.assertEqual([b - a for a, b in zip(step_ticks, step_ticks[1:])], [4] * 4)

    def test_empty_path(self):
        self.assertEqual(build_paced_walk_queue((0, 0), [], movement_speed_milli=2500), [])

    def test_rejects_non_adjacent_cell_in_path(self):
        with self.assertRaises(ValueError):
            build_paced_walk_queue((0, 0), [(1, 0), (3, 0)], movement_speed_milli=2500)

    def test_rejects_zero_length_step(self):
        with self.assertRaises(ValueError):
            build_paced_walk_queue((0, 0), [(0, 0)], movement_speed_milli=2500)

    def test_rejects_bad_speed_and_rate(self):
        with self.assertRaises(ValueError):
            build_paced_walk_queue((0, 0), [(1, 0)], movement_speed_milli=0)
        with self.assertRaises(ValueError):
            build_paced_walk_queue(
                (0, 0), [(1, 0)], movement_speed_milli=2500, tick_rate_hz=0
            )


class PacingAcrossQueuesTests(unittest.TestCase):
    """A queue sent right after the last Step must not draw movement_cooldown."""

    def test_next_tick_after_step_owes_full_waits(self):
        q = build_paced_walk_queue(
            (1, 0), [(2, 0)], movement_speed_milli=2500, ticks_since_last_step=1
        )
        self.assertEqual(q, [WAIT, WAIT, WAIT, STEP_RIGHT])

    def test_partly_elapsed(self):
        q = build_paced_walk_queue(
            (1, 0), [(2, 0), (3, 0)], movement_speed_milli=2500, ticks_since_last_step=3
        )
        self.assertEqual(q, [WAIT, STEP_RIGHT, WAIT, WAIT, WAIT, STEP_RIGHT])

    def test_period_elapsed_steps_at_once(self):
        for elapsed in (4, 50):
            with self.subTest(elapsed=elapsed):
                q = build_paced_walk_queue(
                    (1, 0), [(2, 0)], movement_speed_milli=2500,
                    ticks_since_last_step=elapsed,
                )
                self.assertEqual(q, [STEP_RIGHT])

    def test_spliced_queues_keep_the_period(self):
        # First queue's Step runs on tick 0; the second's first intent on tick 1.
        first = build_paced_walk_queue((0, 0), [(1, 0)], movement_speed_milli=2500)
        second = build_paced_walk_queue(
            (1, 0), [(2, 0), (3, 0)], movement_speed_milli=2500, ticks_since_last_step=1
        )
        q = first + second
        step_ticks = [i for i, intent in enumerate(q) if intent["verb"] == "Step"]
        self.assertEqual([b - a for a, b in zip(step_ticks, step_ticks[1:])], [4, 4])

    def test_rejects_non_positive_elapsed(self):
        with self.assertRaises(ValueError):
            build_paced_walk_queue(
                (0, 0), [(1, 0)], movement_speed_milli=2500, ticks_since_last_step=0
            )


if __name__ == "__main__":
    unittest.main()
