"""A39: Solve state, Compose, and use_block plan ops."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.fragments import fragment_set_complete, holds_whole
from agentrealm_agent.item_table import FragmentMeta, InventorySupply, supplies_from_list
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, goal_done
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.solve import SolveState, solve_op, solve_outcome
from agentrealm_agent.world import WorldModel


def grid(rows: list[str], at=(0, 0)) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(g, g)
    w.terrain_center, w.terrain_map = at, 1
    return w


def frag(
    sid: int,
    code: str,
    into: str,
    slot: int,
    *,
    piece_count: int = 2,
    missing: tuple[int, ...] = (),
) -> InventorySupply:
    return InventorySupply(
        sid,
        code,
        FragmentMeta(composes_into=into, piece_count=piece_count, slot=slot, missing_slots=missing),
    )


def ctx(w: WorldModel, plan: Plan | None) -> PlayContext:
    return PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0), plan=plan)


class FragmentMetaTest(unittest.TestCase):
    def test_inventory_parses_fragment(self):
        held = supplies_from_list(
            [
                {
                    "id": 3,
                    "supply_subtype_code": "key_fragment_a",
                    "fragment": {"composes_into": "rusty_key", "piece_count": 2, "slot": 1, "missing_slots": [2]},
                }
            ]
        )
        self.assertEqual(len(held), 1)
        meta = held[0].fragment
        self.assertEqual(meta.composes_into, "rusty_key")
        self.assertEqual(meta.missing_slots, (2,))

    def test_complete_when_no_missing_slots(self):
        held = [
            frag(1, "a", "rusty_key", 1, missing=()),
            frag(2, "b", "rusty_key", 2, missing=()),
        ]
        self.assertTrue(fragment_set_complete(held, "rusty_key"))
        self.assertFalse(holds_whole(held, "rusty_key"))


class GoalDoneSolveTest(unittest.TestCase):
    def test_compose_done_when_whole_held(self):
        op = {"op": "compose", "composes_into": "rusty_key"}
        w = grid(["."])
        w.held_supplies = [InventorySupply(9, "rusty_key")]
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))

    def test_use_block_done_when_door_opened(self):
        op = {"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}
        w = grid([".D."], at=(1, 0))
        w.apply_events(
            [{"tick": 1, "events": [{"kind": "BlockChanged", "map_id": 1, "x": 2, "y": 0, "block_type": "dirt"}]}]
        )
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))


class SolveStateTest(unittest.TestCase):
    def test_guard_needs_plan_op(self):
        w = grid(["..."])
        state = SolveState()
        self.assertFalse(state.guard(w, ctx(w, None)))
        plan = Plan([{"op": "compose", "composes_into": "rusty_key"}], dict(PARAM_DEFAULTS))
        self.assertTrue(state.guard(w, ctx(w, plan)))

    def test_compose_sends_compose_intent(self):
        w = grid(["."])
        w.held_supplies = [
            frag(1, "frag_a", "rusty_key", 1, missing=()),
            frag(2, "frag_b", "rusty_key", 2, missing=()),
        ]
        plan = Plan([{"op": "compose", "composes_into": "rusty_key"}], dict(PARAM_DEFAULTS))
        out = solve_outcome(w, Memory(), Policy(kind="scripted"), plan, never_attack=[])
        self.assertEqual(out.intents[0]["verb"], "Compose")
        self.assertEqual(sorted(out.intents[0]["supply_ids"]), [1, 2])

    def test_compose_missing_fragments_yields(self):
        w = grid(["."])
        w.held_supplies = [frag(1, "frag_a", "rusty_key", 1, missing=(2,))]
        plan = Plan([{"op": "compose", "composes_into": "rusty_key"}], dict(PARAM_DEFAULTS))
        out = solve_outcome(w, Memory(), Policy(kind="scripted"), plan, never_attack=[])
        self.assertIsNone(out.intents)
        self.assertIn("missing", out.reason)

    def test_use_block_arms_and_uses(self):
        w = grid([".D."], at=(1, 0))
        w.held_supplies = [InventorySupply(5, "rusty_key")]
        plan = Plan([{"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}], dict(PARAM_DEFAULTS))
        out = solve_outcome(w, Memory(), Policy(kind="scripted"), plan, never_attack=[])
        self.assertEqual([i["verb"] for i in out.intents], ["Arm", "Use"])
        self.assertEqual(out.intents[1]["target"], {"kind": "block", "x": 2, "y": 0})

    def test_use_block_walks_into_reach(self):
        w = grid(["...."], at=(0, 0))
        w.held_supplies = [InventorySupply(5, "rusty_key")]
        w.armed_code = "rusty_key"
        plan = Plan([{"op": "use_block", "x": 3, "y": 0, "code": "rusty_key"}], dict(PARAM_DEFAULTS))
        out = solve_outcome(w, Memory(), Policy(kind="scripted"), plan, never_attack=[])
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))

    def test_plan_compose_not_dropped_by_replan(self):
        from agentrealm_agent.pathing import replan

        w = grid(["."])
        plan = Plan([{"op": "compose", "composes_into": "rusty_key"}], dict(PARAM_DEFAULTS))
        replan(w, Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0), set(), set(), plan=plan)
        self.assertEqual(solve_op(plan)["op"], "compose")


if __name__ == "__main__":
    unittest.main()
