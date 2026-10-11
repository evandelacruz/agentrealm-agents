"""Fight or flee is one decision per engagement, and a target's price carries the danger round it (A94).

Rebuilt offline on open ground: Fight, Flee and Retreat read one decision,
the win estimate against a bar, which changes only when the estimate does;
and a hostile that comes out to fight us holds the ground out to where it
did, so Gather and Detour price a pile there as held.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.engagement import BREAK_EVEN, fight_bar, sync_engagement
from agentrealm_agent.hostile_ground import Danger, danger
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.detour import detour_find
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_agent.survival import win_ratio
from agentrealm_agent.world import Entity, Sighting, WorldModel, chebyshev

MAP = 1
CODE = "fake_biter"
SWING_TICKS = 15  # survival.HOSTILE_ATTACK_INTERVAL_TICKS


def world(at=(10, 10), health=10, size=60) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=8, health=health, max_health=10, lives=9)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.hostile_types.add(("npc", CODE))
    w.attack_range = 1
    return w


def ctx(on_hostile="fight", pickup=False) -> PlayContext:
    policy = Policy(kind="scripted", goals=["explore"], on_hostile=on_hostile, hostile=["npc"], pickup=pickup)
    return PlayContext(Memory(), policy, random.Random(0), params=dict(PARAM_DEFAULTS))


def biter(npc_id=7, at=(11, 10)) -> Entity:
    return Entity("npc", npc_id, at, code=CODE)


def hit(w: WorldModel, npc_id=7, amount=2) -> None:
    w.apply_events([{"tick": w.tick, "events": [
        {"kind": "Attacked", "actor_kind": "npc", "actor_id": npc_id},
        {"kind": "Damaged", "amount": amount, "source_kind": "npc", "source_id": npc_id},
    ]}])
    w.learn_threat([
        {"kind": "Attacked", "actor_kind": "npc", "actor_id": npc_id, "tick": w.tick},
        {"kind": "Damaged", "amount": amount, "source_kind": "npc", "source_id": npc_id, "tick": w.tick},
    ], [])
    w.health -= amount


def swings(out) -> bool:
    return any(i["verb"] == "Use" for i in out.intents or [])


class OneDecisionTest(unittest.TestCase):
    def test_a_biter_that_keeps_pace_is_fought_or_fled_as_the_estimate_says(self):
        """It stays adjacent and hits every swing: Fight and Flee never take
        turns between hits, and a swing goes out only while the decision is fight."""
        w, c = world(), ctx()
        npc = biter()
        w.entities = [npc]
        log = []
        for _ in range(120):
            w.tick += 1
            if w.tick % SWING_TICKS == 0 and w.health > 2:
                hit(w)
            out = dispatch(w, c)
            e = c.memory.engagement
            log.append((w.tick, out.state, swings(out), e.fight if e else None))
            if out.intents and out.intents[0]["verb"] == "SetPosition":
                w.pos = (out.intents[0]["x"], out.intents[0]["y"])
                npc.pos = (w.pos[0] + 1, w.pos[1])  # keeps pace
        for tick, _, swung, fight in log:
            self.assertTrue(not swung or fight, (tick, log))
        flips = [log[i][0] for i in range(1, len(log)) if log[i][3] != log[i - 1][3]]
        for tick in flips:
            self.assertEqual(tick % SWING_TICKS, 0, f"the decision changed between hits at {tick}: {log}")
        states = [s for _, s, _, _ in log]
        turns = sum(1 for a, b in zip(states, states[1:]) if (a == "Fight") != (b == "Fight"))
        self.assertLessEqual(turns, len(flips), log)

    def test_a_member_stepping_out_of_range_keeps_the_decision(self):
        w, c = world(), ctx()
        w.entities = [biter(), biter(8, (11, 11))]
        first = sync_engagement(w, c.memory, c.policy, c.params)
        w.entities[1].pos = (14, 14)  # out of hostile_range
        again = sync_engagement(w, c.memory, c.policy, c.params)
        self.assertEqual(again.group, first.group)
        self.assertEqual(again.ratio, first.ratio)

    def test_a_new_hostile_joining_decides_again(self):
        w, c = world(), ctx()
        w.entities = [biter()]
        self.assertTrue(sync_engagement(w, c.memory, c.policy, c.params).fight)
        w.entities.append(biter(8, (9, 10)))
        e = sync_engagement(w, c.memory, c.policy, c.params)
        self.assertEqual(e.group, {("npc", 7), ("npc", 8)})
        self.assertFalse(e.fight, "a pair is more than the knife takes at the margin")

    def test_the_guards_read_the_decision_and_change_nothing(self):
        from agentrealm_agent.states.fight import should_fight
        from agentrealm_agent.states.flee import should_flee

        w, c = world(), ctx()
        w.entities = [biter()]
        e = sync_engagement(w, c.memory, c.policy, c.params)
        for _ in range(3):
            should_fight(w, c)
            should_flee(w, c)
        self.assertIs(c.memory.engagement, e)

    def test_the_engagement_ends_when_nobody_is_in_range_or_hitting(self):
        w, c = world(), ctx()
        w.entities = [biter()]
        sync_engagement(w, c.memory, c.policy, c.params)
        w.entities = [biter(at=(20, 10))]
        w.tick += 100
        self.assertIsNone(sync_engagement(w, c.memory, c.policy, c.params))
        self.assertIsNone(c.memory.engagement)


class CannotOutrunTest(unittest.TestCase):
    """Running that cannot open distance only takes free hits: a fight won outright beats it."""

    def setUp(self):
        self.w, self.c = world(health=7), ctx()
        self.w.entities = [biter()]
        self.ratio = win_ratio(7, self.w.entities, self.w.threat, None)
        bar = fight_bar(self.w, self.c.policy, self.c.params, outrun_failed=False)
        self.assertTrue(BREAK_EVEN < self.ratio <= bar, (self.ratio, bar))

    def test_flees_while_running_may_work(self):
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Flee")
        self.assertFalse(swings(out), out.reason)

    def test_fights_once_running_fails_and_fight_takes_over(self):
        m = self.c.memory
        m.state, m.flee_since, m.flee_gaps = "Flee", self.w.tick, [(self.w.tick, 1)]
        self.w.tick += 5
        hit(self.w, amount=0)  # a swing that hit for nothing: odds, not health
        self.w.health = 7
        out = dispatch(self.w, self.c)
        self.assertTrue(m.engagement.cannot_outrun)
        self.assertTrue(m.engagement.fight)
        self.assertTrue(swings(out), out.reason)
        self.w.tick += 1
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Fight", "Fight reads the same decision")

    def test_flee_policy_fights_back_on_the_same_decision(self):
        c = ctx(on_hostile="flee")
        m = c.memory
        m.state, m.flee_since, m.flee_gaps = "Flee", self.w.tick, [(self.w.tick, 1)]
        self.w.tick += 5
        hit(self.w, amount=0)
        self.w.health = 7
        out = dispatch(self.w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(out.reason, "not outrunning npc 7: fight npc 7")


    def test_cornered_by_a_hitter_we_lose_to_swings_rather_than_stand(self):
        """No step away and no refuge: standing only takes free hits."""
        w, c = world(health=10), ctx(on_hostile="flee")
        w.threat.record(("npc", CODE), 3)
        w.threat.hits[("npc", CODE)] = 50  # hits for 3 nearly every swing: we lose to it, above the floor of 6
        for x in range(9, 12):
            for y in range(9, 12):
                if (x, y) not in ((10, 10), (11, 10)):
                    w.view.tiles[(x, y)] = "wall"
        w.entities = [biter()]
        m = c.memory
        m.state, m.flee_since, m.flee_gaps = "Flee", w.tick, [(w.tick, 1)]
        w.tick += 5
        hit(w, amount=1)
        out = dispatch(w, c)
        self.assertFalse(m.engagement.fight)
        self.assertEqual(out.reason, "not outrunning npc 7: fight npc 7")


def guard_post(w: WorldModel, npc: Entity, home) -> None:
    """``npc`` keeps a post at ``home``, seen just now."""
    w.sightings[(npc.kind, npc.id)] = Sighting(npc, MAP, w.tick, home, post=True, noted=w.tick)


def gem(supply_id: int, at) -> Entity:
    return Entity("supply", supply_id, at, code="gem")


class TurnedBackTest(unittest.TestCase):
    """A hostile that came out to fight us holds the ground out to where it did."""

    POST = (30, 10)
    PILE = (26, 10)

    def setUp(self):
        self.w, self.c = world(at=(24, 10)), ctx(on_hostile="flee", pickup=True)
        self.guard = biter(7, self.POST)
        guard_post(self.w, self.guard, self.POST)

    def turned_back(self) -> None:
        """The guard comes out to us at (24, 10), then goes home and out of view."""
        self.w.pos = self.w.terrain_center = (24, 10)
        self.guard.pos = (25, 10)
        self.w.entities = [self.guard]
        sync_engagement(self.w, self.c.memory, self.c.policy, self.c.params)
        self.guard.pos = self.POST
        self.w.pos = self.w.terrain_center = (10, 10)
        self.w.entities = [gem(50, self.PILE)]
        self.w.tick += 100
        sync_engagement(self.w, self.c.memory, self.c.policy, self.c.params)

    def test_its_reach_stretches_to_where_it_came_for_us(self):
        self.turned_back()
        self.assertEqual(self.w.sightings[("npc", 7)].reach, chebyshev((24, 10), self.POST))

    def test_gather_does_not_go_back_for_the_pile_it_turned_us_from(self):
        w = self.w
        w.entities = [gem(50, self.PILE)]
        w.pos = w.terrain_center = (10, 10)
        m = Memory()
        gather_outcome(w, m, self.c.policy, op={"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", self.PILE), "before: the pile is outside the post's ground")
        self.turned_back()
        m = Memory()
        gather_outcome(w, m, self.c.policy, op={"op": "gather_gems", "count": 5})
        self.assertNotEqual(m.gather_target, ("pile", self.PILE))

    def test_detour_does_not_go_back_for_it_either(self):
        self.turned_back()
        m = self.c.memory
        m.goal, m.path = "travel", [(x, 10) for x in range(11, 40)]
        self.assertIsNone(detour_find(self.w, self.c))


class PricedTargetTest(unittest.TestCase):
    """Danger at a target counts in its price: a nearer pile on faded ground loses to a clear one."""

    def test_gather_takes_the_clear_pile_over_a_nearer_priced_one(self):
        w = world(at=(10, 10))
        w.entities = [gem(50, (13, 10)), gem(51, (10, 14))]
        priced = Danger(priced=(((13, 10), 2, 6),))
        m = Memory()
        policy = ctx().policy
        with mock.patch("agentrealm_agent.states.gather.danger", return_value=priced):
            gather_outcome(w, m, policy, op={"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (10, 14)))
        m = Memory()
        gather_outcome(w, m, policy, op={"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (13, 10)), "unpriced, the nearer one")

    def test_detour_counts_the_price_against_its_allowance(self):
        w, c = world(at=(10, 10)), ctx(pickup=True)
        w.entities = [gem(50, (14, 17))]  # 3 steps more than a walk east along y = 10
        m = c.memory
        m.goal, m.path = "travel", [(x, 10) for x in range(11, 40)]
        self.assertIsNotNone(detour_find(w, c, danger(w, c.policy)))
        self.assertIsNone(detour_find(w, c, Danger(priced=(((14, 17), 1, 6),))))


class NearGroundTest(unittest.TestCase):
    """Ground near what a hostile holds is priced, and a pile there competes with grass on that price."""

    POST = (30, 10)

    def setUp(self):
        self.policy = ctx(on_hostile="flee").policy
        self.w = world(at=(20, 10))
        self.guard = biter(7, self.POST)
        guard_post(self.w, self.guard, self.POST)
        self.w.entities = [gem(50, (25, 10))]  # 5 from the post: outside its ground, near it
        self.w.view.tiles[(20, 16)] = "grass"  # 6 steps off, far from the post

    def pick(self) -> tuple:
        m = Memory()
        gather_outcome(self.w, m, self.policy, op={"op": "gather_gems", "count": 5})
        return m.gather_target

    def test_grass_further_off_beats_a_pile_near_a_post(self):
        self.assertEqual(self.pick(), ("grass", (20, 16)))

    def test_with_no_hostile_known_the_pile_is_taken(self):
        self.w.sightings.clear()
        self.assertEqual(self.pick(), ("pile", (25, 10)))

    def test_a_pile_counts_its_sure_gem_against_grass(self):
        self.w.sightings.clear()
        self.w.entities = [gem(50, (20, 22))]  # 12 steps, against grass at 6
        self.assertEqual(self.pick(), ("pile", (20, 22)))


class RepriceTest(unittest.TestCase):
    def test_a_hostile_joining_a_fight_has_committed_targets_priced_again(self):
        w, c = world(), ctx()
        m = c.memory
        m.gather_target, m.goal, m.path = ("pile", (14, 10)), "gather", [(11, 10), (12, 10)]
        w.entities = [biter()]
        sync_engagement(w, m, c.policy, c.params)
        self.assertIsNone(m.gather_target)
        self.assertEqual((m.goal, m.path), ("", []))


if __name__ == "__main__":
    unittest.main()
