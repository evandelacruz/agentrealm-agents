"""M6 dual poll cadence: calm spacing vs urgent every-tick polling."""

import unittest

from agentrealm_agent.brain import Memory, choose_call
from agentrealm_agent.config import Policy
from agentrealm_agent.poll_cadence import (
    calm_poll_interval,
    gate_tick_call,
    health_dropping,
    hostile_within,
    is_urgent,
    should_poll_tick,
)
from agentrealm_agent.world import Entity, WorldModel


def world(at=(5, 5), tick=20) -> WorldModel:
    w = WorldModel(character_id=42, map_id=1, pos=at, perception=25, tick=tick)
    w.terrain_center, w.terrain_map = at, 1
    w.entities_tick = tick
    return w


def calm_mem(**kw) -> Memory:
    defaults = dict(need_self=False, need_position=False, last_poll_tick=20, calm_poll_interval=7)
    defaults.update(kw)
    return Memory(**defaults)


class PollCadenceTest(unittest.TestCase):
    def test_calm_interval_is_between_four_and_ten(self):
        seen = {calm_poll_interval(t, 1) for t in range(200)}
        self.assertGreaterEqual(min(seen), 4)
        self.assertLessEqual(max(seen), 10)

    def test_calm_skips_tick_until_interval_elapses(self):
        w = world(tick=25)
        m = calm_mem(last_poll_tick=20, calm_poll_interval=7)
        self.assertFalse(should_poll_tick(w, Policy(), alarm=False, last_poll_tick=20, calm_interval=7, prev_health=10, health=10))
        w.tick = 27
        self.assertTrue(should_poll_tick(w, Policy(), alarm=False, last_poll_tick=20, calm_interval=7, prev_health=10, health=10))

    def test_urgent_when_hostile_within_three_blocks(self):
        w = world(at=(0, 0))
        w.entities = [Entity("npc", 1, (3, 0))]
        self.assertTrue(hostile_within(w, Policy(hostile=["npc"])))
        w.entities = [Entity("npc", 1, (4, 0))]
        self.assertFalse(hostile_within(w, Policy(hostile=["npc"])))

    def test_urgent_on_alarm_damage_and_health_drop(self):
        w = world(tick=30)
        self.assertTrue(health_dropping(w, alarm=True, last_poll_tick=20, prev_health=10, health=10))
        w.recent_damage = [(25, 2)]
        self.assertTrue(health_dropping(w, alarm=False, last_poll_tick=20, prev_health=10, health=10))
        self.assertTrue(health_dropping(w, alarm=False, last_poll_tick=20, prev_health=10, health=8))

    def test_hostile_forces_every_tick_even_if_calm_spacing_not_met(self):
        w = world(tick=22)
        w.entities = [Entity("npc", 9, (2, 2))]
        m = calm_mem(last_poll_tick=20, calm_poll_interval=9)
        pol = Policy(hostile=["npc"])
        self.assertEqual(gate_tick_call(w, pol, alarm=False, last_poll_tick=20, calm_interval=9, prev_health=None, health=None), "tick")

    def test_choose_call_returns_skip_in_calm_between_polls(self):
        w = world(tick=24)
        m = calm_mem(last_poll_tick=20, calm_poll_interval=7)
        pol = Policy(kind="scripted", entity_refresh=20)
        self.assertEqual(choose_call(w, m, pol), "skip")

    def test_choose_call_returns_tick_when_calm_interval_met(self):
        w = world(tick=27)
        m = calm_mem(last_poll_tick=20, calm_poll_interval=7)
        pol = Policy(kind="scripted", entity_refresh=20)
        self.assertEqual(choose_call(w, m, pol), "tick")

    def test_cadence_transitions_calm_to_urgent_to_calm(self):
        w = world(tick=22)
        m = calm_mem(last_poll_tick=20, calm_poll_interval=8)
        pol = Policy(hostile=["npc"])
        self.assertEqual(choose_call(w, m, pol), "skip")
        w.entities = [Entity("npc", 3, (4, 4))]
        self.assertTrue(is_urgent(w, pol, alarm=False, last_poll_tick=20, prev_health=10, health=10))
        self.assertEqual(choose_call(w, m, pol), "tick")
        w.entities = []
        w.recent_damage = []
        w.tick = 23
        m.last_poll_tick = 23
        m.alarm = False
        self.assertFalse(is_urgent(w, pol, alarm=False, last_poll_tick=23, prev_health=10, health=10))
        w.tick = 24
        self.assertEqual(choose_call(w, m, pol), "skip")


if __name__ == "__main__":
    unittest.main()
