"""A166: an Equip that sends nothing finishes or asks at once, and any state
that holds with no intent asks the planner within ``HELD_ASK_SECONDS``."""

import unittest
from unittest import mock

from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.fording import WATER_MARK_TICKS, note_would_strand
from agentrealm_adk.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.ask import op_key
from agentrealm_agent.states.base import StateOutcome
from agentrealm_agent.states.dispatch import HELD_ASK_SECONDS
from tests.test_fording import SWORD, arm, ctx_for, lake

ASHORE = (6, 1)  # the far shore, dirt on the map, with the lake at x 2..4


def no_code_equip() -> Plan:
    return Plan([{"op": "equip", "why": "re-arm the weapon once ashore"}], dict(PARAM_DEFAULTS))


def asks(m: Memory) -> list[dict]:
    return [s for s in m.strategist_signals if s["trigger"] == "no_way_on"]


class EquipAshoreTest(unittest.TestCase):
    def test_ashore_with_the_raft_armed_a_no_code_equip_arms_the_weapon(self):
        w = lake(at=ASHORE, armed="raft")
        out = dispatch(w, ctx_for(Memory(), no_code_equip()))
        self.assertEqual((out.state, out.intents), ("Equip", [arm(SWORD)]), out.reason)

    def test_a_would_strand_mark_filed_against_the_shore_lapses(self):
        w = lake(at=ASHORE, armed="raft")
        m = Memory()
        note_would_strand(w, m, "would_strand")  # filed against a cell already left the water for
        plan = no_code_equip()
        out = dispatch(w, ctx_for(m, plan))
        self.assertEqual(out.state, "Equip", "the cut Arm is Equip's to ask about, no hold below it")
        self.assertIsNone(out.intents)
        (ask,) = asks(m)
        self.assertIn("would strand", ask["why"])
        self.assertEqual(ask["op"], plan.current())
        w.tick += WATER_MARK_TICKS
        out = dispatch(w, ctx_for(m, plan))
        self.assertEqual((out.state, out.intents), ("Equip", [arm(SWORD)]), out.reason)

    def test_nothing_to_equip_finishes_the_op(self):
        w = lake(at=ASHORE, armed="bronze_sword", held=("bronze_sword",))
        plan = no_code_equip()
        out = dispatch(w, ctx_for(Memory(), plan))
        self.assertIsNone(plan.current(), out.reason)


class HoldsNothing:
    """A state that claims every round and sends nothing."""

    name = "HoldsNothing"

    def guard(self, world, ctx):
        return True

    def done(self, world, ctx):
        return False

    def act(self, world, ctx):
        return StateOutcome(None, "nothing to send", state=self.name, wait=True, progress=False)


class SilentHoldTest(unittest.TestCase):
    def hold(self, w, m, plan, seconds):
        ticks = seconds * plan.tick_hz
        with mock.patch("agentrealm_agent.states.dispatch._states", return_value=(HoldsNothing(),)):
            for _ in range(ticks + 1):
                dispatch(w, ctx_for(m, plan))
                w.tick += 1

    def test_a_hold_with_no_intent_asks_once_with_the_state_and_its_reason(self):
        w, m, plan = lake(at=ASHORE), Memory(), no_code_equip()
        self.hold(w, m, plan, HELD_ASK_SECONDS - 1)
        self.assertEqual(asks(m), [], "a short hold is no stall")
        self.hold(w, m, plan, 5)
        (ask,) = asks(m)
        self.assertIn("HoldsNothing", ask["why"])
        self.assertIn("nothing to send", ask["why"])
        self.assertEqual(ask["op"], plan.current())

    def test_not_while_an_ask_waits_for_the_planner(self):
        w, m, plan = lake(at=ASHORE), Memory(), no_code_equip()
        m.no_way_on = (w.map_id, w.pos, "equip: no move", op_key(plan.current()))
        self.hold(w, m, plan, HELD_ASK_SECONDS + 2)
        self.assertEqual(asks(m), [], "the planner has not answered yet")
        m.answered_ask = m.no_way_on  # the reply came, and the hold goes on
        self.hold(w, m, plan, HELD_ASK_SECONDS + 1)
        self.assertEqual(len(asks(m)), 1)

    def test_a_reflex_holding_the_round_is_no_stall(self):
        w, m, plan = lake(at=ASHORE), Memory(), no_code_equip()

        class Downed(HoldsNothing):
            name = "Downed"

        with mock.patch("agentrealm_agent.states.dispatch._states", return_value=(Downed(),)):
            for _ in range((HELD_ASK_SECONDS + 2) * plan.tick_hz):
                dispatch(w, ctx_for(m, plan))
                w.tick += 1
        self.assertEqual(asks(m), [])


if __name__ == "__main__":
    unittest.main()
