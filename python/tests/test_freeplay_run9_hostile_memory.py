"""Free-play run 9 offline: a persisted knowledge base walled Gather in, and kept every fight refused (A85).

Run 9 was the first run to start from a knowledge base earlier runs had
filled. Two things it carried, or failed to carry, stopped the agent:

1. Remembered hostiles never expired: hostiles from earlier runs held the
   grass round town, and Gather made 1 cut all run. A hostile keeps no post
   (Manual §11 Hostiles, A162): one remembered out of view holds ground
   round the cell it was last seen on, for ``SIGHTING_TICKS`` when it was
   moving, and until that cell is in sight with it gone when it stood still
   there (blocked), however long the break. Looking is the evidence.
2. The threat table was not saved, so every run started with every type
   unmeasured, and the win estimate refused every unmeasured type. Each
   type's largest hit and its hits and misses are now saved and loaded, and
   a type never measured is priced at the world's base attack power.
"""

from __future__ import annotations

import json
import unittest

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.hostile_ground import hostiles_within, known_reach
from agentrealm_adk.knowledge_base.hostiles import SIGHTINGS_KEY, load_hostiles, save_hostiles
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_adk.world import SIGHTING_TICKS, Entity, WorldModel

MAP = 1
CODE = "fake_gnasher"
CELL = (20, 10)  # where an earlier run last saw it
LAST_SEEN = 1000  # the tick it was last seen there
OP = {"op": "gather_gems", "count": 5}


def loses(w, policy, params) -> bool:
    """With hostiles in range, an engagement under ``on_hostile = fight`` would not start as a fight."""
    import dataclasses

    from agentrealm_adk.engagement import would_fight
    from agentrealm_adk.survival import combat_group

    fight = dataclasses.replace(policy, on_hostile="fight")
    return bool(combat_group(w, fight)) and not would_fight(w, fight, params)


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


def kb_with_sighting(**row) -> KnowledgeBase:
    """A knowledge base an earlier run left: the hostile's type, and where it last saw one standing still."""
    kb = KnowledgeBase.empty("fake-world")
    kb.npc_types[CODE] = {"hostile": True}
    base = {"code": CODE, "map_id": MAP, "x": CELL[0], "y": CELL[1], "tick": LAST_SEEN, "still": True}
    kb.extra[SIGHTINGS_KEY] = {"9": {**base, **row}}
    return kb


def through_json(kb: KnowledgeBase) -> KnowledgeBase:
    return KnowledgeBase.from_dict(kb.world_code, json.loads(json.dumps(kb.to_dict())))


# Grass only round the remembered cell, out of sight from where we stand.
HELD_GRASS = {(CELL[0] + dx, CELL[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}

# Ticks in a break of 2.4 hours at 10 ticks/s.
LONG_BREAK = 86400


class RememberedHostilesTest(unittest.TestCase):
    def gather_at(self, tick: int, at=(12, 10), **row) -> Memory:
        w = field(at=at, grass=HELD_GRASS)
        load_hostiles(kb_with_sighting(**row), w)
        see(w, [], tick)
        m = Memory()
        gather_outcome(w, m, policy(), op=OP)
        return m

    def test_a_still_hostile_seen_just_now_blocks_gather(self):
        """Its grass is barred: Gather goes for unexplored ground instead."""
        self.assertEqual(self.gather_at(LAST_SEEN + 10).gather_target[0], "frontier")

    def test_a_still_hostile_nobody_looked_for_holds_its_cell_over_a_long_break(self):
        """Blocked where it stood: time away does not move it."""
        self.assertEqual(self.gather_at(LAST_SEEN + LONG_BREAK).gather_target[0], "frontier")

    def test_a_moving_hostile_lets_its_cell_go_after_sighting_ticks(self):
        m = self.gather_at(LAST_SEEN + SIGHTING_TICKS + 1, still=False)
        self.assertEqual(m.gather_target[0], "grass")
        self.assertIn(m.gather_target[1], HELD_GRASS)

    def test_its_cell_in_sight_and_empty_lets_it_go(self):
        w = field()
        load_hostiles(kb_with_sighting(), w)
        w.pos = (CELL[0], CELL[1] + 4)  # its cell in sight, nobody on it
        see(w, [], LAST_SEEN + LONG_BREAK)
        self.assertEqual(w.sightings, {})
        self.assertEqual(hostiles_within(w, policy(), CELL, 1), [])

    def test_an_older_agents_post_loads_as_still_where_it_was_last_seen(self):
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types[CODE] = {"hostile": True}
        kb.extra[SIGHTINGS_KEY] = {"9": {
            "code": CODE, "map_id": MAP, "x": 17, "y": 2, "tick": LAST_SEEN,
            "home": list(CELL), "post": True, "reach": 4, "strength": 0.8, "noted": LAST_SEEN, "spells": 2,
        }}
        w = field(at=(2, 30))
        load_hostiles(kb, w)
        s = w.sightings[("npc", 9)]
        self.assertEqual((s.entity.pos, s.still), ((17, 2), True))
        self.assertEqual([c for c, _ in known_reach(w, policy())], [(17, 2)])

    def test_what_is_saved_is_where_it_was_last_seen_and_whether_it_stood_still(self):
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types[CODE] = {"hostile": True}
        w = field(at=(2, 30))
        load_hostiles(kb, w)
        guard = Entity("npc", 9, CELL, CODE)
        for t in range(0, 60):
            see(w, [guard], t)
        see(w, [], 61)
        save_hostiles(kb, w)
        row = through_json(kb).extra[SIGHTINGS_KEY]["9"]
        self.assertEqual(row, {"code": CODE, "map_id": MAP, "x": 20, "y": 10, "tick": 59, "still": True})


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

    def test_a_miss_on_another_tick_is_not_cancelled_by_a_hit(self):
        """Review on #180: events with no tick of their own take their group's,
        so a hit on one tick and a miss on the next are told apart."""
        w = field(at=(5, 5))
        see(w, [Entity("npc", 3, (6, 5), self.WEAK[1])], 0)
        groups = [
            {"tick": 1, "events": [
                {"kind": "Attacked", "actor_kind": "npc", "actor_id": 3},
                {"kind": "Damaged", "amount": 1, "source_kind": "npc", "source_id": 3},
            ]},
            {"tick": 16, "events": [{"kind": "Attacked", "actor_kind": "npc", "actor_id": 3}]},
            {"tick": 31, "events": [{"kind": "Damaged", "amount": 1, "source_kind": "npc", "source_id": 3}]},
        ]
        w.learn_threat(w.apply_events(groups), w.entities)
        self.assertEqual((w.threat.hits[self.WEAK], w.threat.misses[self.WEAK]), (2, 1))

    def test_a_measured_type_loads_and_allows_a_weak_fight(self):
        kb = KnowledgeBase.empty("fake-world")
        save_hostiles(kb, self.measured_run())
        self.assertEqual(kb.npc_types[self.WEAK[1]], {"hostile": True, "max_hit": 1, "hits": 1, "misses": 1})
        w = field(at=(5, 5))
        w.armed_code = "pocket_knife"
        load_hostiles(through_json(kb), w)
        self.assertTrue(w.threat.measured(self.WEAK))
        see(w, [Entity("npc", 4, (6, 5), self.WEAK[1])], 100)
        self.assertFalse(loses(w, policy(), dict(PARAM_DEFAULTS)))

    def test_a_type_measured_to_hit_hard_is_still_refused(self):
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types["fake_brute"] = {"hostile": True, "max_hit": 6, "hits": 8}
        w = field(at=(5, 5))
        w.armed_code = "pocket_knife"
        load_hostiles(kb, w)
        see(w, [Entity("npc", 4, (6, 5), "fake_brute")], 100)
        self.assertTrue(loses(w, policy(), dict(PARAM_DEFAULTS)))

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
