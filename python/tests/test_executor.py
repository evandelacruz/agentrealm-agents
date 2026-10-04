"""M6 paced movement queues."""

import unittest

from agentrealm_agent.executor import pace_steps, step_direction, ticks_per_move


class ExecutorTest(unittest.TestCase):
    def test_ticks_per_move_default_pace(self):
        self.assertEqual(ticks_per_move(10, 2500), 4)

    def test_pace_path_inserts_waits(self):
        intents, queued = pace_steps(
            (0, 0),
            [(1, 0), (2, 0)],
            movement_speed=2500,
            tick_hz=10,
            horizon_ticks=40,
        )
        self.assertEqual(queued, [(1, 0), (2, 0)])
        self.assertEqual(
            intents,
            [
                {"verb": "Step", "direction": "right"},
                {"verb": "Wait"},
                {"verb": "Wait"},
                {"verb": "Wait"},
                {"verb": "Step", "direction": "right"},
            ],
        )

    def test_horizon_caps_steps(self):
        _, queued = pace_steps(
            (0, 0),
            [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)],
            movement_speed=2500,
            tick_hz=10,
            horizon_ticks=8,
        )
        self.assertEqual(len(queued), 2)

    def test_step_direction_diagonal(self):
        self.assertEqual(step_direction((1, 1), (2, 0)), "up_right")


if __name__ == "__main__":
    unittest.main()
