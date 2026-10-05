"""The loop's handling of round-trip results, events, and failures, with a fake server."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import Decision, Memory, choose_call, use_on
from agentrealm_agent.client import ApiError, Client
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.executor import step_landing
from agentrealm_agent.runner import Runner
from agentrealm_agent.threat import type_key_for_entity
from agentrealm_agent.world import Entity, WorldModel


class FakeClient:
    """Answers tick submits from a script and records what was sent.

    Each submit gets queue_id qN, N counting submits from 1, as the front
    answers with the request's id (docs/API.md Round Trip).
    """

    def __init__(self, ticks: list[dict], queue_ids: bool = True):
        self.ticks = list(ticks)
        self.queue_ids = queue_ids
        self.sent: list[tuple[list[dict] | None, int | None]] = []

    def tick(self, cid, intents, *, snapshot_version=None):
        self.sent.append((intents, snapshot_version))
        r = dict(self.ticks.pop(0))
        if intents is not None and self.queue_ids:
            r["queue_id"] = f"q{len(self.sent)}"
        return r


def rejected(queue_id: str, code: str, category: str, tick: int = 1) -> dict:
    return {"tick": tick, "queue_id": queue_id, "index": 0, "outcome": "rejected",
            "rejection": {"category": category, "code": code, "retryability": "transient"}}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, client, pol: Policy) -> Runner:
        cfg = CharacterConfig("T", "default", "test", "sandbox", pol, Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for y in range(2):
            for x in range(5):
                w.view.tiles[(x, y)] = "dirt"
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        return r

    def test_rejected_step_rolls_back_and_is_not_resubmitted(self):
        # Reflex 1 (PLAN.md): a rejected Step does not enter the block.
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [rejected("q1", "block_occupied", "occupied", 10)]},
            {"tick": 13, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, pol)
        r.tick()
        intents0, _ = fake.sent[0]
        self.assertEqual(intents0[0], {"verb": "Step", "direction": "right"})
        self.assertGreater(len(intents0), 1, "paced multi-intent queue")
        self.assertEqual(r.world.pos, (0, 0), "movement resolves from results, not assumed")

        r.tick()  # queue held; rejection for the first Step arrives
        self.assertIsNone(fake.sent[1][0])
        self.assertEqual(r.world.pos, (0, 0))
        self.assertTrue(r.mem.need_position)

        r.world.apply_position({"map_id": 7, "x": 0, "y": 0})
        r.mem.need_position = False
        r.tick()
        self.assertNotEqual(fake.sent[2][0], fake.sent[0][0], "replan avoids the rejected step")

    def test_a_result_for_another_queue_is_not_applied(self):
        # docs/API.md Intent Results: a result names its queue_id and index, so
        # one for a queue other than the pending intent's is not its result.
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [{"tick": 10, "queue_id": "q1", "index": 0, "outcome": "applied_no_effect"}]},
            {"tick": 12, "window_remaining_ms": 0,
             "intent_results": [rejected("q0", "block_occupied", "occupied", 9)]},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        r.tick()
        self.assertEqual(r.world.pos, (1, 0))
        r.tick()
        self.assertFalse(r.mem.need_position)
        self.assertEqual(r.world.pos, (1, 0))

    def test_a_stale_queue_result_beside_a_new_submit_is_not_adopted(self):
        # Only the queue_id our own submit was answered with is ours. A late
        # result for an earlier queue, arriving on the response to a new
        # submit, is not applied, whether or not that response names a queue.
        stale = rejected("q0", "block_occupied", "occupied", 9)
        for queue_ids in (True, False):
            with self.subTest(queue_ids=queue_ids):
                fake = FakeClient([{"tick": 10, "window_remaining_ms": 0, "intent_results": [stale]}],
                                  queue_ids=queue_ids)
                r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
                r.tick()
                self.assertGreater(len(fake.sent[0][0]), 1)
                self.assertEqual(r.mem.pending_queue, "q1" if queue_ids else None)
                self.assertFalse(r.mem.need_position)
                self.assertIsNone(r.mem.nav.wait_tile)
                self.assertIsNotNone(r.mem.pending_intents, "our queue is still awaited")
                self.assertEqual(r.mem.pending_next_index, 0)

    def test_nothing_to_do_leaves_the_queue_as_it_is(self):
        # docs/API.md Intent Queue: a request without intents leaves the held
        # queue; an empty list would clear it.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0}])
        r = self.runner(fake, Policy(goals=["hold"]))
        r.tick()
        self.assertEqual(fake.sent, [(None, None)])

    def test_queue_events_about_us_carry_no_subject(self):
        # docs/API.md, Events: Attacked, Damaged, and Died on a queue happen to
        # the owner and never carry subject_id.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0, "events_by_tick": [
            {"tick": 11, "events": [{"tick": 11, "kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": 3},
                                    {"tick": 11, "kind": "Died", "cause": "npc"}]}]}])
        r = self.runner(fake, Policy(goals=["hold"]))
        r.tick()
        self.assertTrue(r.mem.alarm)
        self.assertTrue(r.mem.need_self and r.mem.need_position)
        self.assertIsNone(r.world.pos)
        self.assertEqual(r.world.recent_damage, [(11, 3)])
        # npc 4 was never perceived: no type to file the hit under.
        self.assertEqual(r.world.threat.by_type, {})

    def test_damage_is_keyed_by_type_from_the_same_observation(self):
        # The observation that first lists the attacker arrives with the
        # Damaged event; the hit is filed under its type, not its id.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0, "events_by_tick": [
            {"tick": 11, "events": [{"tick": 11, "kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": 3}]}],
            "observation": {"version": 2, "delta": {"entities": {"npcs": {"added": [
                {"id": 4, "x": 1, "y": 0, "npc_type_code": "gristlewick"}]}}}}}])
        r = self.runner(fake, Policy(goals=["hold"]))
        r.tick()
        npc = next(e for e in r.world.entities if e.kind == "npc" and e.id == 4)
        self.assertEqual(type_key_for_entity(npc), ("npc", "gristlewick"))
        self.assertEqual(r.world.threat.by_type, {("npc", "gristlewick"): 3})
        self.assertEqual(r.world.threat.damage_per_hit(type_key_for_entity(npc)), 3)

    def test_a_step_sent_as_we_die_is_not_assumed(self):
        # Died forgets the position; the step sent that round trip has no
        # position to land from, so the model stays unplaced until re-read.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0, "events_by_tick": [
            {"tick": 11, "events": [{"tick": 11, "kind": "Died", "cause": "npc"}]}]}])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        self.assertIsNotNone(fake.sent[0][0])
        self.assertIsNone(r.world.pos)
        self.assertIsNone(r.world.map_id)


    def test_back_to_back_use_queues_honor_weapon_cooldown(self):
        # A1: the next Use queue opens with Waits still owed after the last one.
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 5}}
        self.assertEqual(r.intents_for(Decision(use, "test")), [use])
        r.mem.last_use_tick = 10
        r.world.tick = 11
        second = r.intents_for(Decision(use, "test"))
        self.assertEqual([i["verb"] for i in second], ["Wait"] * 9 + ["Use"])

    def test_say_carries_speech_cooldown(self):
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        say = {"verb": "Say", "text": "hi", "target": {"kind": "character", "character_id": 3}}
        self.assertEqual(r.intents_for(Decision(say, "test")), [say])
        r.mem.last_speech_tick = 20
        r.world.tick = 25
        second = r.intents_for(Decision(say, "test"))
        self.assertEqual([i["verb"] for i in second], ["Wait"] * 5 + ["Say"])

    def test_say_to_an_npc_is_remembered_by_its_top_level_npc_id(self):
        # Say names an NPC by npc_id, not a target (API rules § Say); an
        # applied one is spoken, a rejected one counts against the item.
        from agentrealm_agent.interest_list import say_key
        from agentrealm_agent.investigation import spoken_npc_ids
        from agentrealm_agent.knowledge_base import KnowledgeBase
        from agentrealm_agent.states.intents import say_to

        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        r.knowledge = KnowledgeBase.empty("sandbox")
        say = say_to(Entity("npc", 4, (1, 1), "helper"))
        self.assertEqual(say, {"verb": "Say", "npc_id": 4, "text": "hello"})
        r.mem.pending = say
        r.on_result({"tick": 20, "outcome": "rejected", "rejection": {"code": "target_out_of_range"}}, 0)
        self.assertEqual(r.mem.investigate_rejections, {say_key(4): 1})
        r.mem.pending = say
        r.on_result({"tick": 21, "outcome": "applied"}, 0)
        self.assertIn(4, spoken_npc_ids(r.knowledge))

    def test_applied_use_and_say_start_their_cooldowns(self):
        # A1: an applied result records its tick; a rejected one leaves the clock alone.
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 5}}
        r.mem.last_use_tick, r.world.tick = 10, 11
        queue = r.intents_for(Decision(use, "test"))
        self.assertFalse(r.on_result({"tick": 20, "outcome": "applied"}, len(queue) - 1))
        self.assertEqual(r.mem.last_use_tick, 20)
        say = {"verb": "Broadcast", "text": "hi"}
        self.assertEqual(r.intents_for(Decision(say, "test")), [say])
        self.assertFalse(r.on_result({"tick": 21, "outcome": "applied"}, 0))
        self.assertEqual(r.mem.last_speech_tick, 21)
        self.assertEqual(r.mem.last_use_tick, 20)

    def test_npc_out_of_range_drops_the_swings_and_replans(self):
        # A45: an NPC that left sight, died or moved out of reach answers the
        # queued npc-target swing target_out_of_range; the rest is dropped and
        # the next poll re-plans from a fresh position.
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        swing = {"verb": "Use", "target": {"kind": "npc", "npc_id": 5}}
        r.mem.pending_intents = [swing, {"verb": "Wait"}, swing, {"verb": "Step", "direction": "left"}]
        r.mem.pending_queue, r.mem.pending_next_index = "q1", 0
        rejection = {"category": "range", "code": "target_out_of_range", "retryability": "precondition"}
        results = [
            {"queue_id": "q1", "index": 0, "tick": 30, "outcome": "applied"},
            {"queue_id": "q1", "index": 1, "tick": 31, "outcome": "applied"},
            {"queue_id": "q1", "index": 2, "tick": 32, "outcome": "rejected", "rejection": rejection},
        ]
        self.assertTrue(r.apply_intent_results(results))
        self.assertEqual(r.mem.last_use_tick, 30)
        self.assertIsNone(r.mem.pending_intents)
        self.assertIsNone(r.mem.pending_queue)
        self.assertTrue(r.mem.need_position)

    def test_rejected_use_does_not_start_the_cooldown(self):
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 5}}
        r.intents_for(Decision(use, "test"))
        rejection = {"category": "state", "code": "target_out_of_range", "retryability": "transient"}
        self.assertTrue(r.on_result({"tick": 30, "outcome": "rejected", "rejection": rejection}, 0))
        self.assertIsNone(r.mem.last_use_tick)
        say = {"verb": "Say", "text": "hi", "target": {"kind": "character", "character_id": 3}}
        r.intents_for(Decision(say, "test"))
        self.assertTrue(r.on_result({"tick": 31, "outcome": "rejected", "rejection": rejection}, 0))
        self.assertIsNone(r.mem.last_speech_tick)

    def test_death_clears_use_and_speech_cooldowns(self):
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        r.mem.last_use_tick, r.mem.last_speech_tick = 10, 12
        r.on_events([{"tick": 13, "kind": "Died", "cause": "npc"}])
        self.assertIsNone(r.mem.last_use_tick)
        self.assertIsNone(r.mem.last_speech_tick)

    def test_cooldown_past_the_horizon_sends_nothing_and_traces_it(self):
        # The Use cannot land inside the horizon: hold it this round trip, say why.
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        r.queue_horizon_ticks = 3
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 5}}
        r.mem.last_use_tick, r.world.tick = 10, 11
        self.assertIsNone(r.intents_for(Decision(use, "test")))
        self.assertIsNone(r.mem.pending)
        self.assertIsNone(r.mem.pending_intents)
        trace = r.cfg.trace_path.read_text()
        self.assertIn('"call": "pace"', trace)
        self.assertIn('"held": {"verb": "Use"', trace)
        # Once the cooldown is within reach, the Use goes.
        r.world.tick = 18
        self.assertEqual([i["verb"] for i in r.intents_for(Decision(use, "test"))], ["Wait"] * 2 + ["Use"])

    def test_non_movement_intent_is_sent_alone(self):
        # Movement, Use, and Say become paced queues; a Take goes as one intent.
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner(fake, Policy(goals=["hold"], pickup=True))
        r.world.entities = [Entity("supply", 5, (1, 0), "apple")]
        r.tick()
        self.assertEqual(fake.sent[0][0], [{"verb": "Take", "supply_id": 5}])
        self.assertEqual(r.mem.pending, {"verb": "Take", "supply_id": 5})
        self.assertIsNone(r.mem.held_queue)

    def test_unmatched_results_do_not_hold_the_queue_forever(self):
        # Results that never name our queue must not stall the character.
        ticks = [{"tick": 10, "window_remaining_ms": 0}]
        ticks += [{"tick": 11 + i, "window_remaining_ms": 0} for i in range(20)]
        fake = FakeClient(ticks)
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        n = len(fake.sent[0][0])
        for _ in range(n + 3):
            r.tick()
        self.assertIsNone(r.mem.held_queue)
        self.assertIsNone(r.mem.pending_intents)
        # We may have walked unseen: re-read position, drop the stale plan.
        self.assertTrue(r.mem.need_position)
        self.assertEqual(r.mem.path, [])
        self.assertIsNone(r.mem.last_step_tick)
        self.assertEqual(choose_call(r.world, r.mem, r.cfg.policy), "position")
        r.tick()
        self.assertIsNotNone(fake.sent[-1][0], "a fresh decision was sent")

    def test_stepping_onto_a_door_cancels_the_rest_of_the_queue(self):
        # A door moves us, so the queued Steps behind it must not run.
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [{"tick": 11, "queue_id": "q1", "index": 0, "outcome": "applied"}]},
            {"tick": 12, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        self.assertGreater(len(fake.sent[0][0]), 1)
        first = fake.sent[0][0][0]["direction"]
        landing = {"right": (1, 0), "down_right": (1, 1)}[first]
        r.world.view.tiles[landing] = "framed_door"  # revealed after planning
        r.tick()
        self.assertTrue(r.mem.need_position)
        self.assertEqual(choose_call(r.world, r.mem, r.cfg.policy), "tick")
        r.tick()
        self.assertEqual(fake.sent[2][0], [], "the held queue is replaced with nothing")
        self.assertFalse(r.mem.cancel_queue)
        self.assertEqual(choose_call(r.world, r.mem, r.cfg.policy), "position")

    def test_back_to_back_queues_keep_the_step_period(self):
        # The next queue opens with the Waits still owed after the last Step,
        # so its first Step never lands inside movement_cooldown.
        applied = [{"tick": 11 + i, "queue_id": "q1", "index": i, "outcome": "applied"} for i in range(5)]
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 15, "window_remaining_ms": 0, "intent_results": applied},
            {"tick": 16, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.queue_horizon_ticks = 5
        r.tick()
        self.assertEqual([i["verb"] for i in fake.sent[0][0]], ["Step", "Wait", "Wait", "Wait", "Step"])
        r.tick()
        self.assertIsNone(fake.sent[1][0])
        self.assertEqual(r.mem.last_step_tick, 15)
        r.tick()
        self.assertEqual([i["verb"] for i in fake.sent[2][0]], ["Wait", "Wait", "Wait", "Step"])

    def test_an_echoed_queue_does_not_restore_a_dropped_hold(self):
        # A rejection or a door drops our queue; a non-empty `queue` on that
        # same response must not put the hold back.
        echo = {"queue_id": "q1", "next_index": 1}
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0, "queue": echo,
             "intent_results": [rejected("q1", "block_occupied", "occupied", 11)]},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        r.tick()
        self.assertIsNone(r.mem.held_queue)

        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0, "queue": echo,
             "intent_results": [{"tick": 11, "queue_id": "q1", "index": 0, "outcome": "applied"}]},
            {"tick": 12, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        landing = {"right": (1, 0), "down_right": (1, 1)}[fake.sent[0][0][0]["direction"]]
        r.world.view.tiles[landing] = "framed_door"
        r.tick()
        self.assertIsNone(r.mem.held_queue)
        r.tick()
        self.assertEqual(fake.sent[2][0], [])

    def test_single_step_fallback_keeps_the_path(self):
        # A target off the path's head is one Step; the path is not trimmed.
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        r.mem.path = [(3, 0), (4, 0)]
        intents = r.intents_for(Decision({"verb": "SetPosition", "x": 1, "y": 0}, "test"))
        self.assertEqual(intents, [{"verb": "Step", "direction": "right"}])
        self.assertEqual(r.mem.path, [(3, 0), (4, 0)])

    def test_walk_queue_stops_before_an_occupied_cell(self):
        # A12: the cost grid prices an NPC at 50 so the plan may run through
        # it; the queue must not Step onto it. The rest of the plan stays.
        r = self.runner(FakeClient([]), Policy(goals=["hold"]))
        r.world.entities = [Entity("npc", 9, (3, 0))]
        r.mem.path = [(1, 0), (2, 0), (3, 0), (4, 0)]
        intents = r.intents_for(Decision({"verb": "SetPosition", "x": 1, "y": 0}, "test"))
        steps = [i for i in intents if i["verb"] == "Step"]
        self.assertEqual(steps, [{"verb": "Step", "direction": "right"}] * 2)
        self.assertEqual(r.mem.path, [(3, 0), (4, 0)])
        self.assertEqual(r.mem.path_blockers, set(), "no blocker queued at send time")

    def test_hostile_in_range_drops_the_held_queue_and_flees(self):
        # Reflex 3 runs every round trip, not only once the queue drains.
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        self.assertIsNotNone(r.mem.held_queue)
        r.world.entities = [Entity("npc", 9, (2, 1))]  # off the path, within hostile_range
        r.tick()
        self.assertIsNotNone(fake.sent[1][0], "the flee replaces the held queue")
        self.assertEqual(fake.sent[1][0][-1], {"verb": "Step", "direction": "down"})
        self.assertEqual(r.mem.pending_queue, "q2")
        self.assertTrue(r.mem.need_position, "results of the dropped queue are no longer read")

    def test_supply_in_reach_drops_the_held_queue_and_takes(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=True))
        r.tick()
        r.world.entities = [Entity("supply", 5, (0, 1))]
        r.tick()
        self.assertEqual(fake.sent[1][0], [{"verb": "Take", "supply_id": 5}])
        self.assertIsNone(r.mem.pending_intents)

    def _walk_cells(self, start, intents):
        pos, cells = start, []
        for i in intents:
            if i["verb"] == "Step":
                pos = step_landing(pos, i["direction"])
                cells.append(pos)
        return cells

    def _goto_held(self, fake, policy=None):
        """A runner holding a paced walk from (0,0) to (4,0) along y=0."""
        r = self.runner(fake, policy or Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        self.assertIn((2, 0), self._walk_cells((0, 0), fake.sent[0][0]))
        self.assertIsNotNone(r.mem.held_queue)
        return r

    def _resend(self, r, fake, at=(0, 0)):
        """Re-read position (the stale path asked for it), then poll."""
        self.assertTrue(r.mem.resend_held_queue)
        self.assertEqual(choose_call(r.world, r.mem, r.cfg.policy), "position")
        fake.position = lambda cid: {"map_id": 7, "x": at[0], "y": at[1]}
        r.step("position")
        r.tick()
        return fake.sent[-1][0]

    def test_block_changed_on_path_resends_the_held_queue(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0, "events_by_tick": [
                {"tick": 11, "events": [
                    {"kind": "BlockChanged", "map_id": 7, "x": 2, "y": 0, "block_type": "wall"},
                ]},
            ]},
            {"tick": 12, "window_remaining_ms": 0},
        ])
        r = self._goto_held(fake)
        r.tick()
        self.assertIsNone(fake.sent[1][0], "BlockChanged marks stale; hold until resend")
        resent = self._resend(r, fake)
        self.assertEqual(resent[0]["verb"], "Step", "stale path replaces the held queue")
        self.assertNotIn((2, 0), self._walk_cells((0, 0), resent))
        self.assertFalse(r.mem.resend_held_queue)

    def test_observation_entity_on_path_resends_the_held_queue(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0, "observation": {"version": 2, "delta": {"entities": {
                "npcs": {"added": [{"id": 9, "x": 2, "y": 0, "npc_type_code": "rat"}]}}}}},
            {"tick": 12, "window_remaining_ms": 0},
        ])
        r = self._goto_held(fake)
        r.tick()
        self.assertIsNone(fake.sent[1][0])
        self.assertNotIn((2, 0), self._walk_cells((0, 0), self._resend(r, fake)))

    def test_entities_read_on_path_resends_the_held_queue(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self._goto_held(fake)
        fake.entities = lambda cid, map_id, *rect: {"tick": 10, "npcs": [{"id": 9, "x": 2, "y": 0}]}
        r.step("entities")
        self.assertNotIn((2, 0), self._walk_cells((0, 0), self._resend(r, fake)))

    def test_terrain_read_on_path_resends_the_held_queue(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self._goto_held(fake)
        fake.terrain = lambda cid, map_id, *rect: {
            "tick": 10, "map_id": 7, "x0": 2, "y0": 0, "width": 1, "height": 1,
            "rows": ["#"], "legend": {"#": {"block_type": "wall"}},
        }
        r.step("terrain")
        self.assertNotIn((2, 0), self._walk_cells((0, 0), self._resend(r, fake)))

    def test_resend_plans_from_the_reread_position_not_the_unread_results(self):
        # The server walked two steps; only the first result has come back
        # when a wall appears ahead. The new walk starts from the re-read
        # position, and the old queue's late results are not applied.
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 13, "window_remaining_ms": 0,
             "intent_results": [{"tick": 11, "queue_id": "q1", "index": 0, "outcome": "applied"}],
             "events_by_tick": [{"tick": 13, "events": [
                 {"kind": "BlockChanged", "map_id": 7, "x": 3, "y": 0, "block_type": "wall"}]}]},
            {"tick": 15, "window_remaining_ms": 0,
             "intent_results": [{"tick": 14, "queue_id": "q1", "index": 3, "outcome": "applied"}]},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.world.movement_speed = 10000  # one Step per tick at 10 Hz
        r.tick_hz = 10
        r.tick()
        self.assertEqual([i["verb"] for i in fake.sent[0][0][:2]], ["Step", "Step"])
        r.tick()
        self.assertEqual(r.world.pos, (1, 0), "only the first result is in")
        self.assertIsNotNone(r.mem.held_queue, "results still outstanding")
        self.assertTrue(r.mem.need_position, "a stale path re-reads position before the replan")
        resent = self._resend(r, fake, at=(2, 0))
        cells = self._walk_cells((2, 0), resent)
        self.assertNotIn((3, 0), cells)
        self.assertEqual(cells[-1], (4, 0))
        self.assertEqual(r.world.pos, (2, 0), "the old queue's late results are ignored")

    def test_unavoidable_blocker_stops_the_queue_once_and_does_not_resend_again(self):
        # A one-wide corridor: a wall read on it leaves no route (an NPC
        # would not: the cost grid prices occupants, A12). The old queue is
        # stopped with one empty submit; later polls do not resend.
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
            {"tick": 12, "window_remaining_ms": 0},
            {"tick": 13, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        for x in range(-1, 6):
            r.world.view.tiles[(x, -1)] = r.world.view.tiles[(x, 1)] = "wall"
        r.tick()
        fake.terrain = lambda cid, map_id, *rect: {
            "tick": 10, "map_id": 7, "x0": 2, "y0": 0, "width": 1, "height": 1,
            "rows": ["#"], "legend": {"#": {"block_type": "wall"}},
        }
        r.step("terrain")
        self.assertEqual(self._resend(r, fake), [], "no route: the old queue is stopped")
        for _ in range(2):
            r.step("terrain")
            r.tick()
            self.assertIsNone(fake.sent[-1][0])
        self.assertFalse(r.mem.resend_held_queue)

    def test_blocker_already_on_the_queue_when_sent_does_not_resend(self):
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self._goto_held(fake)
        r.mem.path_blockers = {(2, 0)}
        r.world.entities = [Entity("npc", 9, (2, 0))]
        r.note_held_path_stale()
        self.assertFalse(r.mem.resend_held_queue)
        r.world.entities = [Entity("npc", 9, (3, 0))]
        r.note_held_path_stale()
        self.assertTrue(r.mem.resend_held_queue, "a new blocker still counts")

    def test_reflex_or_death_clears_a_pending_resend(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self._goto_held(fake)
        r.world.entities = [Entity("npc", 9, (2, 0))]
        r.note_held_path_stale()
        r.drop_held_queue()
        self.assertFalse(r.mem.resend_held_queue)
        r.mem.resend_held_queue = True
        r.on_events([{"kind": "Died"}])
        self.assertFalse(r.mem.resend_held_queue)

    def test_tick_posts_last_applied_snapshot_version(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0, "observation": {"version": 7, "unchanged": True}},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["hold"]))
        r.world.snapshot_version = 5
        r.tick()
        self.assertEqual(fake.sent[0][1], 5)
        r.tick()
        self.assertEqual(fake.sent[1][1], 7)

    def test_no_reflex_leaves_the_held_queue_and_plan_alone(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False, on_hostile="ignore"))
        r.tick()
        path, held = list(r.mem.path), r.mem.held_queue
        r.world.entities = [Entity("npc", 9, (2, 1))]
        r.tick()
        self.assertIsNone(fake.sent[1][0])
        self.assertEqual(r.mem.held_queue, held)
        self.assertEqual(r.mem.path, path, "the held queue's steps are not planned twice")


    def test_level_clear_ceremony_and_gems_reach_the_trace(self):
        ceremony = {"level_number": 2, "max_health_gain": 5}
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 20, "window_remaining_ms": 0, "level_clear_ceremony": ceremony},
        ])
        r = self.runner(fake, Policy(goals=["wander"], pickup=False, on_hostile="ignore"))
        r._level_timer.overworld = 1
        r._level_timer.note_map(1, 0, 0.0)
        r.world.gems = 6
        r.tick()
        r.world.tick = 20
        r.tick()
        last = json.loads(r.cfg.trace_path.read_text().splitlines()[-1])
        self.assertEqual(last["gems"], 6)
        self.assertEqual(last["level_clear_ceremony"], ceremony)
        self.assertIn("level_duration_s", last)
        self.assertIn("level_duration_ticks", last)

class NeverAttackRunnerTest(RunnerTest):
    """The executor drops a Use on a never_attack target, whatever decided it (A8)."""

    def runner_with_directives(self, client, text: str | None) -> Runner:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "T.directives.toml"
        if text is not None:
            path.write_text(text)
        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(goals=["hold"]),
                              Path(tmp.name) / "T.toml")
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        base = self.runner(client, Policy(goals=["hold"]))
        r.world, r.mem = base.world, base.mem
        r.world.entities = [Entity("character", 5, (1, 0))]
        return r

    def test_use_on_a_forbidden_target_is_replaced_by_wait(self):
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner_with_directives(fake, 'never_attack = ["character"]\n')
        with mock.patch("agentrealm_agent.runner.decide",
                        return_value=Decision(use_on(r.world.entities[0]), "fight")):
            r.tick()
        self.assertEqual(fake.sent[0][0], [{"verb": "Wait"}])
        self.assertIsNone(r.mem.pending, "a dropped Use is not awaited")

    def test_a_paced_use_on_a_forbidden_target_is_replaced_by_one_wait(self):
        # A Use still owing cooldown is sent behind Waits; the guard drops the
        # whole queue, so no stale paced queue is left awaiting its results.
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner_with_directives(fake, 'never_attack = ["character"]\n')
        r.mem.last_use_tick = r.world.tick
        with mock.patch("agentrealm_agent.runner.decide",
                        return_value=Decision(use_on(r.world.entities[0]), "fight")):
            r.tick()
        self.assertEqual(fake.sent[0][0], [{"verb": "Wait"}])
        self.assertIsNone(r.mem.pending_intents)
        self.assertIsNone(r.mem.held_queue)

    def test_use_on_an_allowed_target_is_sent(self):
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner_with_directives(fake, 'never_attack = ["goblin"]\n')
        use = use_on(r.world.entities[0])
        with mock.patch("agentrealm_agent.runner.decide", return_value=Decision(use, "fight")):
            r.tick()
        self.assertEqual(fake.sent[0][0], [use])

    def test_a_changed_directives_file_takes_effect_on_reload(self):
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner_with_directives(fake, None)
        self.assertEqual(r.directives.directives.never_attack, [])
        r.directives.path.write_text('never_attack = ["character"]\n')
        self.assertTrue(r.directives.maybe_reload())
        with mock.patch("agentrealm_agent.runner.decide",
                        return_value=Decision(use_on(r.world.entities[0]), "fight")):
            r.tick()
        self.assertEqual(fake.sent[0][0], [{"verb": "Wait"}])

    def test_directives_reach_the_state_machine(self):
        # A22: the goal stack is read by Gather through decide's directives.
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner_with_directives(fake, 'goals = ["gather_gems:5"]\n')
        with mock.patch("agentrealm_agent.runner.decide", return_value=Decision(None, "hold")) as decide:
            r.tick()
        self.assertTrue(decide.called)
        self.assertEqual(decide.call_args.kwargs["directives"].goals, ["gather_gems:5"])


    def test_directive_params_reach_decide(self):
        # Retreat and Flee read the directives' survival params (A9).
        fake = FakeClient([{"tick": 10, "window_remaining_ms": 0}])
        r = self.runner_with_directives(fake, "params = { retreat_hits = 4 }\n")
        with mock.patch("agentrealm_agent.runner.decide", return_value=Decision(None, "hold")) as dec:
            r.tick()
        self.assertEqual(dec.call_args.kwargs["params"]["retreat_hits"], 4)

class NetworkTest(unittest.TestCase):
    def test_network_failure_is_retried_not_fatal(self):
        # A refused or reset connection is retryable like a 503: it must not end
        # the character's loop.
        with self.assertRaises(ApiError) as cm:
            Client("http://127.0.0.1:9", "k", timeout=1).self_(1)
        self.assertTrue(cm.exception.network)
        stop = threading.Event()
        stop.set()
        fake = FakeClient([])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)):
            cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(), Path("t.toml"))
            r = Runner(cfg, fake, 1, stop, out=lambda _: None)
            retry_at = r.on_error("tick", cm.exception)
            r.trace.close()
        self.assertGreater(retry_at, 0)


if __name__ == "__main__":
    unittest.main()
