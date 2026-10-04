"""M6 executor pacing helpers."""

import unittest

from agentrealm_agent.executor import (
    DEFAULT_WEAPON_COOLDOWN_TICKS,
    SPEECH_INTERVAL_TICKS,
    build_attack_queue,
    pace_speech,
    pace_uses,
    wait,
)


def use_block(x: int, y: int) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": x, "y": y}}


def say(text: str, npc_id: int) -> dict:
    return {"verb": "Say", "text": text, "target": {"kind": "character", "character_id": npc_id}}


class PaceUsesTest(unittest.TestCase):
    def test_two_uses_default_cooldown(self):
        q = pace_uses([use_block(1, 0), use_block(1, 0)])
        self.assertEqual(q[0], use_block(1, 0))
        self.assertEqual(q[1 + DEFAULT_WEAPON_COOLDOWN_TICKS - 1], use_block(1, 0))
        self.assertEqual(len(q), 1 + (DEFAULT_WEAPON_COOLDOWN_TICKS - 1) + 1)
        self.assertTrue(all(x == wait() for x in q[1 : 1 + DEFAULT_WEAPON_COOLDOWN_TICKS - 1]))

    def test_custom_cooldown(self):
        q = pace_uses([use_block(0, 0), use_block(0, 0)], cooldown_ticks=4)
        self.assertEqual(len(q), 1 + 3 + 1)
        self.assertEqual(q[0]["verb"], "Use")
        self.assertEqual(q[4]["verb"], "Use")

    def test_empty(self):
        self.assertEqual(pace_uses([]), [])


class PaceSpeechTest(unittest.TestCase):
    def test_two_says(self):
        a, b = say("hi", 1), say("again", 1)
        q = pace_speech([a, b])
        self.assertEqual(q[0], a)
        self.assertEqual(q[SPEECH_INTERVAL_TICKS], b)
        self.assertEqual(len(q), 1 + (SPEECH_INTERVAL_TICKS - 1) + 1)

    def test_broadcast_paced_like_say(self):
        bcast = {"verb": "Broadcast", "text": "hello"}
        q = pace_speech([say("a", 2), bcast])
        self.assertEqual(q[0]["verb"], "Say")
        self.assertEqual(q[SPEECH_INTERVAL_TICKS]["verb"], "Broadcast")


class BuildAttackQueueTest(unittest.TestCase):
    def test_retreat_appended_when_it_fits(self):
        retreat = [{"verb": "Step", "direction": "up"}] * 3
        q = build_attack_queue([use_block(1, 1)], retreat, poll_interval_ticks=40)
        self.assertEqual(q[0]["verb"], "Use")
        self.assertEqual(q[-3:], retreat)

    def test_caps_to_poll_interval_and_keeps_retreat(self):
        uses = [use_block(1, 1)] * 3
        retreat = [{"verb": "Step", "direction": "left"}] * 4
        # One paced use is 1 intent; three uses with cooldown 10 -> 1 + 9 + 1 + 9 + 1 = 21
        q = build_attack_queue(uses, retreat, poll_interval_ticks=12, weapon_cooldown_ticks=10)
        self.assertLessEqual(len(q), 12)
        self.assertEqual(q[-4:], retreat)

    def test_drops_extra_attacks_before_retreat(self):
        uses = [use_block(0, 0)] * 5
        retreat = [{"verb": "Step", "direction": "down"}]
        q = build_attack_queue(uses, retreat, poll_interval_ticks=8, weapon_cooldown_ticks=10)
        self.assertLessEqual(len(q), 8)
        self.assertEqual(q[-1], retreat[0])
        self.assertEqual(sum(1 for i in q if i["verb"] == "Use"), 1)

    def test_no_retreat_bounded_by_poll(self):
        uses = [use_block(0, 0)] * 4
        q = build_attack_queue(uses, None, poll_interval_ticks=15, weapon_cooldown_ticks=10)
        self.assertLessEqual(len(q), 15)

    def test_poll_interval_must_be_positive(self):
        with self.assertRaises(ValueError):
            build_attack_queue([use_block(0, 0)], None, poll_interval_ticks=0)


if __name__ == "__main__":
    unittest.main()
