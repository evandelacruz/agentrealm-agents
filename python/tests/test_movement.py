import unittest

from executor.movement import (
    build_paced_walk_queue,
    direction_between,
    ticks_per_step,
)


class MovementPacingTests(unittest.TestCase):
    def test_ticks_per_step_base_speed(self):
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed=2.5), 4)

    def test_ticks_per_step_faster(self):
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed=5.0), 2)
        self.assertEqual(ticks_per_step(tick_rate_hz=10, movement_speed=10.0), 1)

    def test_direction_between_diagonal(self):
        self.assertEqual(direction_between((2, 2), (3, 1)), "up_right")

    def test_paced_queue_two_steps_at_base_speed(self):
        q = build_paced_walk_queue(
            (0, 0),
            [(1, 0), (2, 0)],
            movement_speed=2.5,
            tick_rate_hz=10,
        )
        self.assertEqual(
            q,
            [
                {"verb": "Step", "direction": "right"},
                {"verb": "Wait"},
                {"verb": "Wait"},
                {"verb": "Wait"},
                {"verb": "Step", "direction": "right"},
            ],
        )

    def test_paced_queue_five_blocks_per_second(self):
        q = build_paced_walk_queue(
            (0, 0),
            [(0, 1), (0, 2)],
            movement_speed=5.0,
        )
        self.assertEqual(
            q,
            [
                {"verb": "Step", "direction": "down"},
                {"verb": "Wait"},
                {"verb": "Step", "direction": "down"},
            ],
        )

    def test_paced_queue_max_speed_no_waits(self):
        q = build_paced_walk_queue(
            (5, 5),
            [(6, 5), (7, 5), (7, 6)],
            movement_speed=10.0,
        )
        self.assertEqual(
            q,
            [
                {"verb": "Step", "direction": "right"},
                {"verb": "Step", "direction": "right"},
                {"verb": "Step", "direction": "down"},
            ],
        )

    def test_empty_path(self):
        self.assertEqual(build_paced_walk_queue((0, 0), [], movement_speed=2.5), [])


if __name__ == "__main__":
    unittest.main()
