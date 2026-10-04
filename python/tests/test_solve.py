"""A39: Solve state, Compose, and use_block plan ops."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.fragments import compose_supply_ids, fragment_set_complete, holds_whole
from agentrealm_agent.item_table import FragmentMeta, InventorySupply, parse_fragment, supplies_from_list
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan, goal_done
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
    missing: tuple[int, ...] | None = (),
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

    def test_slot_zero_is_kept(self):
        meta = parse_fragment({"composes_into": "k", "piece_count": 2, "slot": 0, "missing_slots": [0]})
        self.assertIsNotNone(meta)
        self.assertEqual((meta.slot, meta.missing_slots), (0, (0,)))

    def test_bool_and_bad_counts_rejected(self):
        self.assertIsNone(parse_fragment({"composes_into": "k", "piece_count": True, "slot": 1}))
        self.assertIsNone(parse_fragment({"composes_into": "k", "piece_count": 2, "slot": False}))
        self.assertIsNone(parse_fragment({"composes_into": "k", "piece_count": 0, "slot": 1}))
        self.assertIsNone(parse_fragment({"composes_into": "k", "piece_count": 2, "slot": -1}))
        self.assertIsNone(parse_fragment({"composes_into": "k", "piece_count": 2, "slot": 1.5}))

    def test_bad_missing_slots_keeps_fragment(self):
        for raw in ("2", [True], ["x"], [-1], {"a": 1}):
            meta = parse_fragment({"composes_into": "k", "piece_count": 2, "slot": 1, "missing_slots": raw})
            self.assertIsNotNone(meta, raw)
            self.assertIsNone(meta.missing_slots, raw)
        meta = parse_fragment({"composes_into": "k", "piece_count": 2, "slot": 1})
        self.assertEqual(meta.missing_slots, ())

    def test_unknown_missing_slots_falls_back_to_slots_held(self):
        held = [frag(1, "a", "k", 0, missing=None), frag(2, "b", "k", 1, missing=None)]
        self.assertTrue(fragment_set_complete(held, "k"))
        self.assertFalse(fragment_set_complete(held[:1], "k"))

    def test_duplicate_slot_does_not_complete_set(self):
        held = [frag(1, "a", "k", 1), frag(2, "a", "k", 1)]
        self.assertFalse(fragment_set_complete(held, "k"))

    def test_duplicate_slot_sent_once(self):
        held = [frag(4, "a", "k", 1), frag(2, "a", "k", 1), frag(3, "b", "k", 2)]
        self.assertTrue(fragment_set_complete(held, "k"))
        self.assertEqual(compose_supply_ids(held, "k"), [2, 3])


class GoalDoneSolveTest(unittest.TestCase):
    def test_compose_done_when_whole_held(self):
        op = {"op": "compose", "composes_into": "rusty_key"}
        w = grid(["."])
        w.held_supplies = [InventorySupply(9, "rusty_key")]
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))

    def test_use_block_done_when_door_opened(self):
        op = {"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}
        w = grid(["..D"], at=(1, 0))
        plan = Plan([op], dict(PARAM_DEFAULTS))
        plan.advance(w)
        self.assertIs(plan.current(), op)
        w.apply_events(
            [{"tick": 1, "events": [{"kind": "BlockChanged", "map_id": 1, "x": 2, "y": 0, "block_type": "dirt"}]}]
        )
        w.apply_events([])  # a later window: the change is still on the tile
        plan.advance(w)
        self.assertIsNone(plan.current())

    def test_use_block_non_door_target_not_done_until_it_changes(self):
        op = {"op": "use_block", "x": 2, "y": 0, "code": "crowbar"}
        w = grid(["..#"], at=(1, 0))
        plan = Plan([op], dict(PARAM_DEFAULTS))
        plan.advance(w)
        self.assertFalse(goal_done(op, w, plan))
        w.view.tiles[(2, 0)] = "rock"  # a terrain read shows another type
        self.assertTrue(goal_done(op, w, plan))

    def test_use_block_door_that_stays_a_door_is_not_done(self):
        op = {"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}
        w = grid(["..D"], at=(1, 0))
        plan = Plan([op], dict(PARAM_DEFAULTS))
        plan.advance(w)
        w.apply_events(
            [{"tick": 1, "events": [{"kind": "BlockChanged", "map_id": 1, "x": 2, "y": 0, "block_type": "framed_door"}]}]
        )
        self.assertFalse(goal_done(op, w, plan))

    def test_use_block_unseen_target_not_done(self):
        op = {"op": "use_block", "x": 9, "y": 9, "code": "rusty_key"}
        w = grid(["."])
        plan = Plan([op], dict(PARAM_DEFAULTS))
        plan.advance(w)
        self.assertFalse(goal_done(op, w, plan))


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

    def test_use_block_rearms_previous_weapon_after_use(self):
        w = grid([".D."], at=(1, 0))
        w.held_supplies = [InventorySupply(5, "rusty_key"), InventorySupply(9, "bronze_sword")]
        w.armed_code = "bronze_sword"
        plan = Plan([{"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}], dict(PARAM_DEFAULTS))
        c = ctx(w, plan)
        out = dispatch(w, c)
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 5})
        w.armed_code = "rusty_key"
        out = dispatch(w, c)
        self.assertEqual(out.intents[0]["verb"], "Use")
        w.view.tiles[(2, 0)] = "framed_door_open"  # the Use changed the block: op done
        out = dispatch(w, c)
        self.assertEqual(out.state, "Solve")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 9}])
        self.assertIsNone(plan.current())
        w.armed_code = "bronze_sword"
        self.assertFalse(SolveState().guard(w, c))

    def test_rearm_sent_once_and_skipped_when_weapon_gone(self):
        w = grid(["."])
        m = Memory(solve_rearm="bronze_sword")
        out = solve_outcome(w, m, Policy(kind="scripted"), Plan([], dict(PARAM_DEFAULTS)), never_attack=[])
        self.assertIsNone(out.intents)
        self.assertIn("not in hand", out.reason)
        self.assertIsNone(m.solve_rearm)

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


class SolveStallTest(unittest.TestCase):
    STALL_TICKS = PLAN_STALL_SECONDS * 10

    def _run(self, w: WorldModel, plan: Plan, ticks: int):
        out = None
        for t in range(ticks + 1):
            w.tick = 100 + t
            out = dispatch(w, ctx(w, plan))
        return out

    def test_stuck_compose_is_dropped_and_plan_moves_on(self):
        w = grid(["....."])
        w.held_supplies = [frag(1, "frag_a", "rusty_key", 1, missing=(2,))]
        explore = {"op": "explore_area", "x": 4, "y": 0, "radius": 1}
        plan = Plan([{"op": "compose", "composes_into": "rusty_key"}, explore], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan))
        self.assertNotEqual(out.state, "Solve")  # yields while stuck (A44)
        self.assertEqual(plan.current()["op"], "compose")
        self._run(w, plan, self.STALL_TICKS)
        self.assertEqual(plan.current(), explore)

    def test_use_block_without_supply_is_dropped(self):
        w = grid(["..D"])
        plan = Plan([{"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}], dict(PARAM_DEFAULTS))
        self._run(w, plan, self.STALL_TICKS)
        self.assertIsNone(plan.current())

    def test_use_without_effect_is_dropped(self):
        w = grid(["..D"], at=(1, 0))
        w.held_supplies = [InventorySupply(5, "rusty_key")]
        w.armed_code = "rusty_key"
        plan = Plan([{"op": "use_block", "x": 2, "y": 0, "code": "rusty_key"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan))
        self.assertEqual(out.intents[0]["verb"], "Use")
        self._run(w, plan, self.STALL_TICKS)
        self.assertIsNone(plan.current())

    def test_walking_resets_the_stall(self):
        w = grid(["...."], at=(0, 0))
        w.held_supplies = [InventorySupply(5, "rusty_key")]
        w.armed_code = "rusty_key"
        plan = Plan([{"op": "use_block", "x": 3, "y": 0, "code": "rusty_key"}], dict(PARAM_DEFAULTS))
        plan.stalled_since_tick = 0
        w.tick = 10_000
        out = solve_outcome(w, Memory(), Policy(kind="scripted"), plan, never_attack=[])
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertIsNone(plan.stalled_since_tick)
        self.assertEqual(plan.current()["op"], "use_block")


if __name__ == "__main__":
    unittest.main()
