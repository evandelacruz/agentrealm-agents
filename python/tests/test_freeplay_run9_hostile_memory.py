"""Free-play run 9 offline: a persisted knowledge base walled Gather in, and kept every fight refused (A85).

Run 9 was the first run to start from a knowledge base earlier runs had
filled. Two things it carried, or failed to carry, stopped the agent:

1. Remembered hostile posts never expired: posts from earlier runs held the
   grass round town, and Gather made 1 cut all run. A post now fades with
   world time unseen, fast while in sight with nobody on it, and stays
   strong for longer when seen again and again. A faded post only prices
   its ground; its reach counts only so far from it.
2. The threat table was not saved, so every run started with every type
   unmeasured, and the win estimate refused every unmeasured type. Each
   type's largest hit and its hits and misses are now saved and loaded, and
   a type never measured is priced at the world's base attack power.
"""

from __future__ import annotations

import json
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.hostile_ground import POST_REACH_CAP, danger, hostiles_within, known_reach
from agentrealm_agent.hostile_memory import SIGHTINGS_KEY, load_hostiles, save_hostiles
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_agent.survival import would_lose
from agentrealm_agent.world import EMPTY_POST_HALF_LIFE_TICKS, POST_HALF_LIFE_TICKS, POST_HOLD_STRENGTH, Entity, WorldModel

MAP = 1
CODE = "fake_gnasher"
POST = (20, 10)
LAST_SEEN = 1000  # the tick an earlier run last saw the guard on its post
OP = {"op": "gather_gems", "count": 5}


def policy() -> Policy:
    return Policy(kind="scripted", goals=[], on_hostile="ignore")


def field(at=(12, 10), grass=(), size=40, perception=6) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=perception)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "grass" if (x, y) in grass else "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.health, w.max_health, w.lives = 10, 10, 10
    w.attack_range = 1
    return w


def see(w: WorldModel, entities: list[Entity], tick: int) -> None:
    w.tick = tick
    w._set_entities(entities, tick)


def kb_with_post(**row) -> KnowledgeBase:
    """A knowledge base an earlier run left: the guard's type, and its post."""
    kb = KnowledgeBase.empty("fake-world")
    kb.npc_types[CODE] = {"hostile": True}
    base = {
        "code": CODE, "map_id": MAP, "x": POST[0], "y": POST[1], "tick": LAST_SEEN,
        "home": list(POST), "post": True, "reach": 0,
    }
    kb.extra[SIGHTINGS_KEY] = {"9": {**base, **row}}
    return kb


def through_json(kb: KnowledgeBase) -> KnowledgeBase:
    return KnowledgeBase.from_dict(kb.world_code, json.loads(json.dumps(kb.to_dict())))


# Grass only round the remembered post, out of view from where we stand.
POST_GRASS = {(POST[0] + dx, POST[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}


class RememberedPostsFadeTest(unittest.TestCase):
    def gather_at(self, tick: int, **row) -> Memory:
        w = field(grass=POST_GRASS)
        load_hostiles(kb_with_post(**row), w)
        see(w, [], tick)
        m = Memory()
        gather_outcome(w, m, policy(), op=OP)
        return m

    def test_a_post_seen_just_now_still_blocks_gather(self):
        """Its grass is barred: Gather walks on to ground clear of it (A82)."""
        m = self.gather_at(LAST_SEEN + 10)
        self.assertEqual(m.gather_target[0], "clear")

    def test_an_aged_post_no_longer_blocks_gather(self):
        m = self.gather_at(LAST_SEEN + 2 * POST_HALF_LIFE_TICKS)
        self.assertEqual(m.gather_target[0], "grass")
        self.assertIn(m.gather_target[1], POST_GRASS)

    def test_a_post_from_long_ago_is_forgotten(self):
        w = field()
        load_hostiles(kb_with_post(), w)
        see(w, [], LAST_SEEN + 4 * POST_HALF_LIFE_TICKS)
        self.assertEqual(w.sightings, {})

    def test_a_post_seen_on_many_spells_stays_strong_for_longer(self):
        m = self.gather_at(LAST_SEEN + 2 * POST_HALF_LIFE_TICKS, spells=3)
        self.assertEqual(m.gather_target[0], "clear")

    def test_seeing_the_guard_again_makes_its_post_strong_again(self):
        w = field(at=(17, 10))
        load_hostiles(kb_with_post(), w)
        t = LAST_SEEN + 2 * POST_HALF_LIFE_TICKS
        see(w, [Entity("npc", 9, POST, CODE)], t)
        w.pos = (2, 10)
        see(w, [], t + 1)
        s = w.sightings[("npc", 9)]
        self.assertGreater(s.strength, 0.99)
        self.assertEqual(s.spells, 2)
        self.assertEqual(len(known_reach(w, policy())), 2)

    def test_its_strength_is_saved_as_it_has_faded(self):
        kb = kb_with_post()
        w = field()
        loaded = load_hostiles(kb, w)
        see(w, [], LAST_SEEN + POST_HALF_LIFE_TICKS)
        save_hostiles(kb, w, loaded)
        row = through_json(kb).extra[SIGHTINGS_KEY]["9"]
        self.assertAlmostEqual(row["strength"], 0.5, places=3)
        self.assertEqual(row["noted"], LAST_SEEN + POST_HALF_LIFE_TICKS)


class AGuardJustSeenStillHoldsItsCellTest(unittest.TestCase):
    """Review on #180: the guard was seen off its post seconds ago, then its
    empty post was looked at until it faded. The post holds nothing; the
    cell the guard was last seen on, out of sight now, still does."""

    def test_its_last_seen_cell_holds_while_the_post_does_not(self):
        w = field(at=(17, 10))
        load_hostiles(kb_with_post(), w)
        roamed = (17, 2)
        see(w, [Entity("npc", 9, roamed, CODE)], LAST_SEEN)  # off its post, in view
        w.pos = (20, 14)  # the post in sight, the roamed cell not
        for t in range(LAST_SEEN + 1, LAST_SEEN + 1 + 2 * EMPTY_POST_HALF_LIFE_TICKS):
            see(w, [], t)
        self.assertLess(w.sightings[("npc", 9)].strength, POST_HOLD_STRENGTH)
        reach = known_reach(w, policy())
        self.assertEqual([c for c, _ in reach], [roamed])
        self.assertEqual(hostiles_within(w, policy(), roamed, 1), [w.sightings[("npc", 9)].entity])
        self.assertEqual(hostiles_within(w, policy(), POST, 1), [])


class FadedGroundIsPricedTest(unittest.TestCase):
    """A faded post adds a bounded price to its grass, never a bar."""

    def pick(self, free: tuple[int, int]) -> tuple[int, int]:
        w = field(at=(17, 10), grass=POST_GRASS | {free}, perception=12)
        load_hostiles(kb_with_post(), w)
        see(w, [], LAST_SEEN + 2 * POST_HALF_LIFE_TICKS)  # strength 0.25: faded
        self.assertGreater(danger(w, policy()).price((POST[0] - 1, POST[1])), 0)
        m = Memory()
        gather_outcome(w, m, policy(), op=OP)
        return m.gather_target[1]

    def test_free_grass_a_little_further_off_comes_first(self):
        self.assertEqual(self.pick((14, 10)), (14, 10))  # 3 off, the post's grass 2 off

    def test_the_posts_grass_wins_when_free_grass_is_far(self):
        self.assertIn(self.pick((2, 10)), POST_GRASS)


class ReachIsBoundedTest(unittest.TestCase):
    def test_a_long_chase_does_not_stretch_a_posts_ground_across_the_map(self):
        w = field(at=(2, 30))
        load_hostiles(kb_with_post(reach=25), w)
        see(w, [], LAST_SEEN + 1)
        self.assertIn((POST, POST_REACH_CAP + 1), known_reach(w, policy()))


def hit_and_miss(w: WorldModel, npc_id: int, tick: int) -> None:
    """``npc_id`` swings at us twice on ``tick`` and on the next: a hit, then a miss."""
    events = [
        {"kind": "Attacked", "actor_kind": "npc", "actor_id": npc_id, "tick": tick},
        {"kind": "Damaged", "amount": 1, "source_kind": "npc", "source_id": npc_id, "tick": tick},
        {"kind": "Attacked", "actor_kind": "npc", "actor_id": npc_id, "tick": tick + 15},
    ]
    w.apply_events([{"tick": tick, "events": events}])
    w.learn_threat(events, w.entities)


class ThreatKeptAcrossRunsTest(unittest.TestCase):
    WEAK = ("npc", "fake_snotling")

    def measured_run(self) -> WorldModel:
        w = field(at=(5, 5))
        see(w, [Entity("npc", 3, (6, 5), self.WEAK[1])], 0)
        hit_and_miss(w, 3, 1)
        return w

    def test_hits_and_misses_are_counted(self):
        w = self.measured_run()
        self.assertEqual(w.threat.by_type[self.WEAK], 1)
        self.assertEqual((w.threat.hits[self.WEAK], w.threat.misses[self.WEAK]), (1, 1))

    def test_a_measured_type_loads_and_allows_a_weak_fight(self):
        kb = KnowledgeBase.empty("fake-world")
        save_hostiles(kb, self.measured_run())
        self.assertEqual(kb.npc_types[self.WEAK[1]], {"hostile": True, "max_hit": 1, "hits": 1, "misses": 1})
        w = field(at=(5, 5))
        w.armed_code = "pocket_knife"
        load_hostiles(through_json(kb), w)
        self.assertTrue(w.threat.measured(self.WEAK))
        see(w, [Entity("npc", 4, (6, 5), self.WEAK[1])], 100)
        self.assertFalse(would_lose(w, policy(), dict(PARAM_DEFAULTS)))

    def test_a_type_measured_to_hit_hard_is_still_refused(self):
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types["fake_brute"] = {"hostile": True, "max_hit": 6, "hits": 8}
        w = field(at=(5, 5))
        w.armed_code = "pocket_knife"
        load_hostiles(kb, w)
        see(w, [Entity("npc", 4, (6, 5), "fake_brute")], 100)
        self.assertTrue(would_lose(w, policy(), dict(PARAM_DEFAULTS)))

    def test_two_characters_add_their_own_counts(self):
        """The knowledge base is shared: each save adds only what its run counted."""
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types[self.WEAK[1]] = {"hostile": True, "max_hit": 2, "hits": 5, "misses": 2}
        first, second = field(at=(5, 5)), field(at=(5, 5))
        load_hostiles(kb, first)
        load_hostiles(kb, second)
        for w in (first, second):
            see(w, [Entity("npc", 3, (6, 5), self.WEAK[1])], 0)
            hit_and_miss(w, 3, 1)
            save_hostiles(kb, w)
        save_hostiles(kb, first)  # a second save adds nothing new
        self.assertEqual(kb.npc_types[self.WEAK[1]], {"hostile": True, "max_hit": 2, "hits": 7, "misses": 4})

    def test_a_broken_count_is_ignored(self):
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types[self.WEAK[1]] = {"hostile": True, "max_hit": "two", "hits": -3, "misses": True}
        w = field()
        load_hostiles(kb, w)
        self.assertFalse(w.threat.measured(self.WEAK))
        self.assertEqual((w.threat.hits, w.threat.misses), ({}, {}))


if __name__ == "__main__":
    unittest.main()
