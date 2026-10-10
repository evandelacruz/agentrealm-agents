"""M6 executor pacing: attack and speech spacing, attack queues with retreat."""

import unittest

from agentrealm_agent.executor import (
    DEFAULT_WEAPON_COOLDOWN_TICKS,
    SPEECH_INTERVAL_TICKS,
    arm_then_use,
    build_attack_queue,
    pace_speech,
    pace_uses,
    queue_horizon_intents,
    wait,
)

HORIZON = queue_horizon_intents()


def use_block(x: int, y: int) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": x, "y": y}}


def say(text: str, npc_id: int) -> dict:
    return {"verb": "Say", "text": text, "target": {"kind": "character", "character_id": npc_id}}


def waits_then_use(n: int) -> list[dict]:
    return [wait()] * n + [use_block(0, 0)]


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

    def test_carried_cooldown_delays_first_use(self):
        self.assertEqual(pace_uses([use_block(0, 0)], ticks_since_last=4), waits_then_use(6))
        self.assertEqual(pace_uses([use_block(0, 0)], ticks_since_last=10), waits_then_use(0))

    def test_other_intents_count_toward_gap(self):
        arm = {"verb": "Arm", "item_id": 1}
        q = pace_uses([use_block(0, 0), arm, use_block(0, 0)])
        self.assertEqual(q, [use_block(0, 0), arm] + waits_then_use(8))


class ArmThenUseTest(unittest.TestCase):
    ARM = {"verb": "Arm", "supply_id": 4}

    def test_use_follows_the_arm_when_ready(self):
        self.assertEqual(arm_then_use(self.ARM, use_block(0, 0), horizon_ticks=HORIZON), [self.ARM, use_block(0, 0)])

    def test_the_arm_counts_toward_the_cooldown(self):
        queue = arm_then_use(self.ARM, use_block(0, 0), horizon_ticks=HORIZON, ticks_since_last_use=3)
        self.assertEqual(queue, [self.ARM] + waits_then_use(DEFAULT_WEAPON_COOLDOWN_TICKS - 3 - 1))

    def test_a_use_past_the_horizon_sends_no_arm_alone(self):
        # Free-play run 3: an Arm sent without its Use left the potion armed.
        self.assertEqual(arm_then_use(self.ARM, use_block(0, 0), horizon_ticks=3, ticks_since_last_use=1), [])


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

    def test_carried_speech_interval(self):
        a = say("hi", 1)
        q = pace_speech([a], ticks_since_last=8)
        self.assertEqual(q, [wait(), wait(), a])


class BuildAttackQueueTest(unittest.TestCase):
    def test_retreat_appended_when_it_fits(self):
        retreat = [{"verb": "Step", "direction": "up"}] * 3
        q = build_attack_queue([use_block(1, 1)], retreat, poll_interval_ticks=40, horizon_ticks=HORIZON)
        self.assertEqual(q[0]["verb"], "Use")
        self.assertEqual(q[-3:], retreat)

    def test_caps_to_poll_interval_and_keeps_retreat(self):
        uses = [use_block(1, 1)] * 3
        retreat = [{"verb": "Step", "direction": "left"}] * 4
        # One paced use is 1 intent; three uses with cooldown 10 -> 1 + 9 + 1 + 9 + 1 = 21
        q = build_attack_queue(uses, retreat, poll_interval_ticks=12, horizon_ticks=HORIZON, weapon_cooldown_ticks=10)
        self.assertLessEqual(len(q), 12)
        self.assertEqual(q[-4:], retreat)

    def test_drops_extra_attacks_before_retreat(self):
        uses = [use_block(0, 0)] * 5
        retreat = [{"verb": "Step", "direction": "down"}]
        q = build_attack_queue(uses, retreat, poll_interval_ticks=8, horizon_ticks=HORIZON, weapon_cooldown_ticks=10)
        self.assertEqual(q, [use_block(0, 0), retreat[0]])

    def test_retreat_follows_last_swing_without_waits(self):
        uses = [use_block(0, 0)] * 3
        retreat = [{"verb": "Step", "direction": "down"}] * 2
        q = build_attack_queue(uses, retreat, poll_interval_ticks=20, horizon_ticks=HORIZON, weapon_cooldown_ticks=10)
        last_use = max(i for i, x in enumerate(q) if x["verb"] == "Use")
        self.assertEqual(q[last_use + 1 :], retreat)

    def test_horizon_caps_long_poll(self):
        uses = [use_block(0, 0)] * 10
        q = build_attack_queue(uses, None, poll_interval_ticks=100, horizon_ticks=25, weapon_cooldown_ticks=10)
        self.assertLessEqual(len(q), 25)
        self.assertEqual(q[-1]["verb"], "Use")

    def test_cooldown_carries_into_next_queue(self):
        retreat = [{"verb": "Step", "direction": "up"}]
        q = build_attack_queue(
            [use_block(0, 0)], retreat, poll_interval_ticks=20, horizon_ticks=HORIZON, ticks_since_last_use=3
        )
        self.assertEqual(q, waits_then_use(7) + retreat)

    def test_no_swing_fits_after_carried_cooldown(self):
        retreat = [{"verb": "Step", "direction": "up"}]
        q = build_attack_queue(
            [use_block(0, 0)], retreat, poll_interval_ticks=4, horizon_ticks=HORIZON, ticks_since_last_use=1
        )
        self.assertEqual(q, retreat)

    def test_no_retreat_bounded_by_poll(self):
        uses = [use_block(0, 0)] * 4
        q = build_attack_queue(uses, None, poll_interval_ticks=15, horizon_ticks=HORIZON, weapon_cooldown_ticks=10)
        self.assertLessEqual(len(q), 15)

    def test_poll_interval_must_be_positive(self):
        with self.assertRaises(ValueError):
            build_attack_queue([use_block(0, 0)], None, poll_interval_ticks=0, horizon_ticks=HORIZON)


if __name__ == "__main__":
    unittest.main()
