"""A120: every op's executor keeps its walk; obstructed, it clears the way or asks and holds.

Evan's rule: "if a goal or target is obstructed we need to try to unobstruct
it, not get distracted". For each op family: the way shut by a block a held
item opens is opened (by the executor's own ``way.open_the_way``, or by
Break at stuck step 2 for the walks on the stuck ladder); shut by one
nothing held opens, the executor raises one ``no_way_on`` saying what blocks
it and what it tried, and holds; the survival reflexes act above the hold;
and the safe default never walks the character off the op's way.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import OP_STATE, Plan
from agentrealm_agent.states import PlayContext, break_state, dispatch
from agentrealm_agent.states.explore import HOLD_THEIR_WALK
from agentrealm_agent.states.fight import FightState
from agentrealm_agent.world import Entity, WorldModel

GLYPHS = {".": "dirt", "#": "wall", "T": "tree"}
LENGTH = 30  # past speech range, so a say op has to walk
END = (LENGTH, 1)
BLOCK = (3, 1)
TREE_OPENED = {"verb": "Use", "target": {"kind": "block", "x": BLOCK[0], "y": BLOCK[1]}}


def corridor(block: str) -> WorldModel:
    """A walled corridor from (1, 1) to ``END`` with ``block`` at ``BLOCK``; matches held and armed (they burn trees)."""
    rows = ["#" * (LENGTH + 2), "#.." + block + "." * (LENGTH - 3) + "#", "#" * (LENGTH + 2)]
    w = WorldModel(character_id=1, map_id=7, pos=(1, 1), perception=LENGTH + 2)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = GLYPHS[ch]
    w.terrain_center, w.terrain_map = (1, 1), 7
    w.alive, w.health, w.max_health, w.lives = True, 10, 10, 5
    w.held_supplies = [InventorySupply(4, "matches")]
    w.armed_code, w.armed_id = "matches", 4
    return w


def fetch(w: WorldModel) -> dict:
    w.entities.append(Entity("supply", 8, END, "apple"))
    return {"op": "fetch_item", "code": "apple"}


def say(w: WorldModel) -> dict:
    w.entities.append(Entity("npc", 4, END, "fake_sage"))
    return {"op": "say", "npc_id": 4, "text": "hello"}


def use_block(w: WorldModel) -> dict:
    w.held_supplies.append(InventorySupply(5, "fake_key"))
    return {"op": "use_block", "x": END[0], "y": END[1], "code": "fake_key"}


def buy(w: WorldModel) -> dict:
    w.entities.append(Entity("supply", 9, END, "fake_torch", gem_price=1))
    w.gems = 5
    return {"op": "buy", "code": "fake_torch"}


def enter_level(w: WorldModel) -> dict:
    w.view.tiles[END] = "door"
    return {"op": "enter_level", "x": END[0], "y": END[1]}


def fight_boss(w: WorldModel) -> dict:
    w.view.tiles[END] = "door"
    return {"op": "fight_boss", "x": END[0], "y": END[1]}


def travel(w: WorldModel) -> dict:
    return {"op": "travel", "to": "point", "x": END[0], "y": END[1]}


# Ops whose executor opens the block itself (``way.open_the_way``), and ops
# on the stuck ladder (A15), where Break opens it at step 2.
OPENS_ITSELF = {"Loot": fetch, "Investigate": say, "Solve": use_block, "Shop": buy}
LADDER = {"Level": enter_level, "Boss": fight_boss, "Travel": travel}


def ctx(op: dict, **policy) -> PlayContext:
    plan = Plan([dict(op)], dict(PARAM_DEFAULTS))
    return PlayContext(Memory(), Policy(kind="scripted", **policy), random.Random(0), plan=plan, directives=default_directives())


def play(w: WorldModel, c: PlayContext, decisions: int) -> list:
    outs = []
    for _ in range(decisions):
        out = dispatch(w, c)
        outs.append(out)
        for i in out.intents or []:
            if i.get("verb") == "SetPosition":
                w.pos = (i["x"], i["y"])
                if c.memory.path[:1] == [w.pos]:
                    c.memory.path = c.memory.path[1:]
        w.tick += 10
    return outs


def asks(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]


class ObstructedThenClearedTest(unittest.TestCase):
    def test_an_executor_with_its_own_walk_opens_the_block_itself(self):
        for owner, make in OPENS_ITSELF.items():
            with self.subTest(owner):
                w = corridor("T")
                c = ctx(make(w))
                outs = play(w, c, 3)
                self.assertEqual([o.state for o in outs], [owner] * 3, [o.reason for o in outs])
                self.assertEqual(outs[0].intents, [{"verb": "SetPosition", "x": 2, "y": 1}], "a step toward the tree")
                self.assertEqual(outs[1].intents, [TREE_OPENED], "the tree burnt")
                self.assertEqual(asks(c), [], "the way was cleared: no ask")

    def test_a_walk_on_the_stuck_ladder_has_break_open_it_and_explore_never_walks(self):
        for owner, make in LADDER.items():
            with self.subTest(owner):
                w = corridor("T")
                c = ctx(make(w))
                outs = play(w, c, 4)
                self.assertIn(TREE_OPENED, [i for o in outs for i in o.intents or []], [o.reason for o in outs])
                self.assertTrue(all(o.state in (owner, "Break") or (o.state, o.intents) == ("Explore", None) for o in outs))
                self.assertEqual(asks(c), [])

    def test_once_open_the_executor_walks_on_through(self):
        w = corridor("T")
        c = ctx(fetch(w))
        play(w, c, 2)
        w.view.tiles[BLOCK] = "dirt"  # the tree burnt down
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents), ("Loot", [{"verb": "SetPosition", "x": 3, "y": 1}]), out.reason)


class TheTargetIsNeverTheBlockOpenedTest(unittest.TestCase):
    def test_step_two_never_opens_the_ops_own_target(self):
        # A use_block target is a block, so no way round reaches it; a held item
        # opening its type must not make it the block on the way (A120).
        from agentrealm_agent.states import way

        w = corridor(".")
        w.view.tiles[END] = "tree"  # matches burn it, but the op is to use the key on it
        c = ctx(use_block(w))
        self.assertIsNone(way.open_the_way(w, c, END, "Solve"))
        w.view.tiles[BLOCK] = "tree"  # a block on the way is still opened
        out = way.open_the_way(w, c, END, "Solve")
        self.assertEqual(out.intents if out else None, [{"verb": "SetPosition", "x": 2, "y": 1}])


class ObstructedThenAskedTest(unittest.TestCase):
    def test_with_nothing_held_that_opens_the_way_every_walking_op_asks_once_and_holds(self):
        for owner, make in {**OPENS_ITSELF, **LADDER}.items():
            with self.subTest(owner):
                w = corridor("#")
                op = make(w)
                c = ctx(op)
                outs = play(w, c, 3)
                for out in outs:
                    self.assertEqual((out.state, out.intents, out.wait), (owner, None, True), out.reason)
                    self.assertTrue(out.reason.endswith("asked the planner"), out.reason)
                self.assertEqual(len(asks(c)), 1, "one ask per cell and reason")
                ask = asks(c)[0]
                self.assertEqual(ask["op"], op)
                self.assertEqual(ask["blocked_by"], "wall at 3,1, which nothing held opens")
                self.assertEqual(ask["tried"], "a way round, then opening a block on the way with a held item")
                self.assertEqual(w.pos, (1, 1), "never walked off the way")

    def test_a_break_block_with_no_way_to_its_block_asks_and_holds(self):
        w = corridor("#")
        w.view.tiles[END] = "tree"
        op = {"op": "break_block", "x": END[0], "y": END[1], "capability": "burn"}
        c = ctx(op)
        with mock.patch.object(break_state, "what_blocks", wraps=break_state.what_blocks) as blocks:
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents, out.wait), ("Break", None, True), out.reason)
            play(w, c, 2)
            self.assertEqual(blocks.call_count, 1, "what blocks it is worked out for the ask, not every held tick")
        self.assertEqual(
            [(a["op"], a["blocked_by"], a["tried"]) for a in asks(c)],
            [(op, "wall at 3,1, which nothing held opens", "a way to the block, round every other")],
        )

    def test_a_hostile_on_the_way_is_named_for_the_planner(self):
        # Fight-or-flee is the engagement's, and a fight it did not choose is the planner's (A114).
        w = corridor("#")
        w.entities.append(Entity("npc", 6, (5, 1), "fake_snotling"))  # beyond the wall, out of range
        w.hostile_types.add(("npc", "fake_snotling"))
        c = ctx(fetch(w), on_hostile="flee", hostile=["npc"], hostile_range=1)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.wait), ("Loot", True), out.reason)
        self.assertEqual(asks(c)[0]["blocked_by"], "hostile fake_snotling 6 at 5,1; wall at 3,1, which nothing held opens")

    def test_an_op_with_nothing_to_work_on_asks_at_once(self):
        w = corridor(".")
        c = ctx({"op": "fetch_item", "code": "apple"})
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.reason), ("Loot", None, "no apple in sight: asked the planner"))
        self.assertNotIn("blocked_by", asks(c)[0])
        # At the op's cell with none there: nothing to work on, not a shut way.
        c = ctx({"op": "fetch_item", "code": "apple", "x": 1, "y": 1})
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.reason), ("Loot", None, "no apple at 1,1: asked the planner"))

    def test_a_fight_op_that_cannot_close_asks_but_leaves_flee_free(self):
        w = corridor(".")
        npc = Entity("npc", 6, END, "fake_snotling")
        w.entities.append(npc)
        c = ctx({"op": "fight", "npc_id": 6})
        with mock.patch("agentrealm_agent.states.fight.engage", side_effect=lambda *a: _no_move()):
            out = dispatch(w, c)
        self.assertIn("Fight: fight npc 6: cannot close: asked the planner", out.yielded)
        self.assertEqual((out.state, out.intents, out.wait), ("Explore", None, True), "the safe default holds for the op")
        self.assertEqual([a["why"] for a in asks(c)], ["fight npc 6: cannot close"])


def _no_move():
    from agentrealm_agent.states.base import StateOutcome

    return StateOutcome(None, "cannot close", state=FightState.name)


class OnlyTheOpsOwnWalkTest(unittest.TestCase):
    def test_another_walks_step_two_never_stands_in_for_the_ask(self):
        # A stuck attempt at step 2 left by another walk (the safe default's) is not the op's way:
        # Loot asks rather than leave the round to Break, which would open that walk's block.
        from agentrealm_agent.navigation import stuck as nav_stuck

        w = corridor("#")
        c = ctx(fetch(w))
        att = nav_stuck.track(c.memory, w, "explore", nav_stuck.Leg((1, LENGTH)))
        att.level = nav_stuck.BREAK
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Loot", None, True), out.reason)
        self.assertEqual(len(asks(c)), 1)


class EveryIdleOpAsksTest(unittest.TestCase):
    def test_an_executor_that_sends_nothing_and_asks_no_one_is_asked_for_once(self):
        # Equip waits for a busy armed slot, sending nothing: the hold asks for it.
        w = corridor(".")
        w.held_supplies.append(InventorySupply(3, "pocket_knife"))
        c = ctx({"op": "equip", "code": "pocket_knife"})
        c.memory.solve_rearm = "fake_torch"
        for _ in range(3):
            c.memory.solve_rearm = "fake_torch"  # Solve still holds the slot
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents, out.wait), ("Explore", None, True), out.reason)
        self.assertEqual([a["why"] for a in asks(c)], ["equip: Equip has no move"], "asked once")

    def test_wait_and_use_item_act_on_their_op_and_ask_no_one(self):
        # A planner's wait is its own hold, and use_item uses, waits out a cooldown or drops with why.
        w = corridor(".")
        c = ctx({"op": "wait", "seconds": 20, "why": "fake reason"})
        self.assertEqual(dispatch(w, c).state, "Wait")
        w.held_supplies.append(InventorySupply(7, "small_potion"))
        c2 = ctx({"op": "use_item", "code": "small_potion"})
        out = dispatch(w, c2)
        self.assertEqual(out.state, "UseItem", out.reason)
        self.assertEqual(asks(c) + asks(c2), [])


class ShutWayIsNotSearchedAgainTest(unittest.TestCase):
    def test_the_hold_reuses_the_shut_way_until_the_ground_changes(self):
        from agentrealm_agent.states import way

        w = corridor("#")
        c = ctx(fetch(w))
        with mock.patch.object(way, "open_the_way", wraps=way.open_the_way) as opened, mock.patch.object(
            way, "what_blocks", wraps=way.what_blocks
        ) as blocks:
            play(w, c, 3)
            self.assertEqual((opened.call_count, blocks.call_count), (1, 1), "searched once, then held")
            w.view.tiles[(LENGTH, 0)] = "wall"  # new ground in view: look again
            play(w, c, 1)
            self.assertEqual((opened.call_count, blocks.call_count), (2, 1), "the ask itself is not repeated")


class SurvivalDuringTheHoldTest(unittest.TestCase):
    def hostile_beside(self, w: WorldModel, c: PlayContext) -> None:
        w.health, w.lives = 500, 10
        w.entities.append(Entity("npc", 5, (2, 1), "fake_snotling"))
        w.threat.record(("npc", "fake_snotling"), 1)
        c.policy = Policy(kind="scripted", on_hostile="fight", hostile=["npc"], hostile_range=2)
        c.params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}

    def test_every_executor_holding_for_the_planner_yields_to_survival(self):
        cases = {**OPENS_ITSELF, **LADDER}
        for owner, make in cases.items():
            with self.subTest(owner):
                w = corridor("#")
                c = ctx(make(w))
                self.assertEqual(dispatch(w, c).state, owner)
                self.hostile_beside(w, c)
                out = dispatch(w, c)
                self.assertIn(out.state, ("Fight", "Flee"), out.reason)
                self.assertTrue(out.intents, out.reason)
                w.entities = [e for e in w.entities if e.id != 5]
                out = dispatch(w, c)
                self.assertEqual((out.state, out.intents, out.wait), (owner, None, True), "the hold resumes")

    def test_ops_with_no_walk_hold_too_and_survival_acts_above_them(self):
        ops = {
            "Wait": {"op": "wait", "seconds": 20, "why": "fake reason"},
            "UseItem": {"op": "use_item", "code": "small_potion"},
        }
        for owner, op in ops.items():
            with self.subTest(owner):
                w = corridor(".")
                w.held_supplies.append(InventorySupply(7, "small_potion"))
                c = ctx(op)
                self.hostile_beside(w, c)
                out = dispatch(w, c)
                self.assertIn(out.state, ("Fight", "Flee"), out.reason)


class SafeDefaultNeverStandsInTest(unittest.TestCase):
    def test_every_executor_but_explore_holds_its_walk(self):
        executors = {s for s in OP_STATE.values() if s is not None}
        self.assertEqual(HOLD_THEIR_WALK, executors - {"Explore"})

    def test_explore_still_explores_with_no_op(self):
        w = WorldModel(character_id=1, map_id=7, pos=(1, 1), perception=3)
        w.view.tiles.update({(x, y): "dirt" for x in range(3) for y in range(3)})  # unseen ground all round
        w.alive = True
        c = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), plan=Plan([], dict(PARAM_DEFAULTS)))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Explore")
        self.assertTrue(out.intents, out.reason)


if __name__ == "__main__":
    unittest.main()
