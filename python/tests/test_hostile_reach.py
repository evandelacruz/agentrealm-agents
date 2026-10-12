"""A128: walks keep off the reach of hostiles seen, a pack is fought whole,
and a walk under attack never stops to wait.

- A walk on an op's behalf prices the ground within a seen hostile's reach
  (plus ``WALK_REACH_MARGIN``) as costly, so it goes round a pack it has
  seen. With no way round, it asks the planner and does not walk through,
  at any level of the stuck ladder.
- The engagement's win estimate counts every hostile that can reach us
  (``survival.can_reach_us``): a pack is weighed whole.
- A reflex walk under attack whose Steps are not sound from every position
  the held queue may have left us on takes the best open Step, or hands to
  Flee's cornered move, rather than stop.
"""

from __future__ import annotations

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent.brain import Decision
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.engagement import decide
from agentrealm_agent.executor.held_queue import STEP_DOUBT, open_step, possible_positions, unconfirmed_send
from agentrealm_agent.executor.movement import step_landing
from agentrealm_agent.hostile_ground import WALK_REACH_MARGIN
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import guided_step
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.survival import win_ratio, worn_defense
from agentrealm_agent.world import Entity, Sighting, WorldModel, chebyshev
from tests.test_runner import FakeClient

GLYPHS = {".": "dirt", "#": "wall"}
POLICY = Policy(kind="scripted", hostile=["npc"], on_hostile="fight")
BAR = POLICY.hostile_range + WALK_REACH_MARGIN  # how far a walk keeps from a hostile seen
STRONG = 6  # a hit the engagement will not take on alone at full health


def world(rows: list[str], at: tuple[int, int]) -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=12)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = GLYPHS[ch]
    w.terrain_center, w.terrain_map = at, 7
    w.alive, w.health, w.max_health = True, 10, 10
    return w


def pack(w: WorldModel, *cells: tuple[int, int], hit: int = 2) -> list[Entity]:
    """Hostiles of a type that has hit us for ``hit`` before, at ``cells``."""
    w.threat.record(("npc", "bogmaw"), hit)
    w.entities = [Entity("npc", 200 + i, p, code="bogmaw") for i, p in enumerate(cells)]
    return w.entities


def travel(x: int, y: int) -> dict:
    return {"op": "travel", "to": "point", "x": x, "y": y}


def ctx(*ops: dict, m: Memory | None = None) -> PlayContext:
    plan = Plan([dict(op) for op in ops], dict(PARAM_DEFAULTS))
    return PlayContext(m or Memory(), POLICY, random.Random(0), plan=plan, directives=default_directives())


def asks(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]


class WalkGoesRoundAPackTest(unittest.TestCase):
    FIELD = ["." * 21] * 13

    def assert_clear_of(self, path: list[tuple[int, int]], hostiles: list[Entity]) -> None:
        for p in path:
            self.assertTrue(all(chebyshev(p, h.pos) > BAR for h in hostiles), f"{p} in reach: {path}")

    def test_travel_routes_round_a_pack_it_has_seen(self):
        w = world(self.FIELD, at=(1, 6))
        hostiles = pack(w, (10, 6), (10, 7), (11, 6))
        c = ctx(travel(19, 6))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual(c.memory.path[-1], (19, 6))
        self.assert_clear_of(c.memory.path, hostiles)

    def test_a_kept_path_is_planned_again_once_a_pack_comes_into_view(self):
        """A pack that comes into view beside the walk is gone round, not walked past."""
        w = world(self.FIELD, at=(1, 6))
        c = ctx(travel(19, 6))
        self.assertEqual(dispatch(w, c).state, "Travel")
        x, y = c.memory.path[8]  # on the way, with nobody seen
        hostiles = pack(w, (x, y), (x + 1, y), (x, y + 1))
        w.tick += 2
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual(c.memory.path[-1], (19, 6))
        self.assert_clear_of(c.memory.path, hostiles)


class NoWayRoundAsksTest(unittest.TestCase):
    CORRIDOR = ["#" * 23, "#" + "." * 21 + "#", "#" * 23]

    def test_a_pack_across_the_only_way_asks_the_planner_and_does_not_walk_through(self):
        w = world(self.CORRIDOR, at=(1, 1))
        pack(w, (12, 1), (13, 1))
        c = ctx(travel(21, 1))
        for _ in range(3):
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), out.reason)
            self.assertEqual(c.memory.path, [], "no path through the pack is kept")
            w.tick += 4
        [ask] = asks(c)
        self.assertEqual(ask["op"], travel(21, 1))
        self.assertIn("hostile bogmaw 200 at 12,1", ask["blocked_by"])

    def test_the_hold_never_climbs_the_stuck_ladder(self):
        """Holding for the planner is no stuck walk: no reveal or break walk goes through the pack."""
        w = world(self.CORRIDOR, at=(1, 1))
        pack(w, (12, 1), hit=STRONG)
        c = ctx(travel(21, 1))
        for _ in range(4):
            out = dispatch(w, c)
            self.assertIsNone(out.intents, out.reason)
            w.tick += 200  # past the walk's progress window, each time
            c.plan.stalled_since_tick = None  # the op's own stall clock is A104's, not this test's

    def test_once_the_pack_moves_off_the_way_the_walk_goes_on(self):
        w = world(self.CORRIDOR, at=(1, 1))
        hostiles = pack(w, (12, 1), hit=STRONG)
        c = ctx(travel(21, 1))
        self.assertIsNone(dispatch(w, c).intents)
        hostiles[0].pos = (40, 1)  # gone down the corridor's far end, out of the way
        w.tick += 4
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (2, 1))


    def test_a_weak_lone_hostile_on_the_way_is_left_to_the_engagement(self):
        """A hostile the engagement would fight holds no walk: the walk goes on and Fight takes it in range."""
        w = world(self.CORRIDOR, at=(1, 1))
        pack(w, (12, 1))
        c = ctx(travel(21, 1))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (2, 1))
        self.assertEqual(asks(c), [])
        w.pos = (10, 1)  # walked up to it: now it is in range
        w.tick += 40
        self.assertEqual(dispatch(w, c).state, "Fight")

    def test_a_strong_lone_hostile_on_the_only_way_asks(self):
        w = world(self.CORRIDOR, at=(1, 1))
        pack(w, (12, 1), hit=STRONG)
        c = ctx(travel(21, 1))
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), out.reason)
        self.assertEqual(len(asks(c)), 1)

    def held_at(self, level: int) -> PlayContext:
        """Travel down the corridor whose walk had already climbed to ``level``
        of the stuck ladder when the pack across it came into view."""
        w = world(self.CORRIDOR, at=(1, 1))
        c = ctx(travel(21, 1))
        self.assertEqual(dispatch(w, c).state, "Travel")
        att = nav_stuck.active(c.memory, w)
        att.level = level
        if level == nav_stuck.BREAK:
            nav_stuck.set_break_target(att, (12, 1), "chop")
        elif level == nav_stuck.REVEAL:
            att.reveal_left = nav_stuck.REVEAL_MOVE_BUDGET
        pack(w, (12, 1), hit=STRONG)
        w.tick += 4
        out = dispatch(w, c)
        self.assertIsNone(out.intents, out.reason)
        self.assertEqual((att.level, nav_stuck.break_target(att)), (nav_stuck.WALK, None), "back to its first level")
        self.assertEqual(c.memory.path, [])
        return c

    def test_an_escalating_walk_is_held_at_every_level(self):
        for level in (nav_stuck.CAUTIOUS, nav_stuck.ALT_ROUTE, nav_stuck.REVEAL, nav_stuck.BREAK):
            with self.subTest(level=level):
                self.held_at(level)

    def test_a_walk_whose_window_ran_out_is_held_too(self):
        w = world(self.CORRIDOR, at=(1, 1))
        c = ctx(travel(21, 1))
        dispatch(w, c)
        att = nav_stuck.active(c.memory, w)
        pack(w, (12, 1), hit=STRONG)
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT + 1  # the window failed before the pack was seen
        att.window_tick = 0
        c.plan.stalled_since_tick = None
        self.assertIsNone(dispatch(w, c).intents)
        self.assertEqual(att.level, nav_stuck.WALK)

    def test_blocked_by_names_a_remembered_hostile_out_of_view(self):
        w = world(self.CORRIDOR, at=(1, 1))
        guard = pack(w, (12, 1))[0]
        w.entities = []  # gone from view; its post still holds the corridor
        w.sightings[(guard.kind, guard.id)] = Sighting(guard, 7, w.tick, (12, 1), post=True)
        c = ctx(travel(21, 1))
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents), ("Travel", None), out.reason)
        [ask] = asks(c)
        self.assertIn("remembered hostile bogmaw 200, out of view, holding ground round 12,1", ask["blocked_by"])


class EscalationPlansAreHeldTest(unittest.TestCase):
    def test_a_level_the_ladder_climbs_to_this_decision_is_held_to_the_reach(self):
        """At WALK with a failed window, the ladder climbs to CAUTIOUS this
        decision: a CAUTIOUS plan into the reach is held at once, not a tick late."""
        w = world(["." * 21] * 9, at=(1, 4))
        m = Memory()
        goal, target = "travel:point", (19, 4)
        straight = [(x, 4) for x in range(2, 20)]
        round_ = [(x, 0 if 3 <= x <= 17 else 1) for x in range(2, 19)] + [(19, 4)]
        reach = {(x, y) for x in range(8, 13) for y in range(2, 7)}

        def plan(att):
            return round_ if att.level == nav_stuck.WALK else straight

        att = nav_stuck.track(m, w, goal, target)
        w.tick = nav_stuck.PROGRESS_TICK_LIMIT + 1  # the window failed
        step = guided_step(m, w, goal, target, set(), plan, reach=reach)
        self.assertIsNone(step)
        self.assertEqual((att.level, m.path), (nav_stuck.WALK, []))


class OpenTheWayKeepsOffTheReachTest(unittest.TestCase):
    """A129's opener: a walk with no step opens the block on its way. Not a
    block it would have to walk into a pack's reach to get to."""

    def clearing(self, pack_beside_the_tree: bool) -> tuple[WorldModel, PlayContext]:
        from tests.test_way_tries import DEST, GOAL, clearing, ctx, given_up

        w, c = clearing(), ctx()
        w.pos = (1, 10)
        if pack_beside_the_tree:
            pack(w, (8, 6), (9, 6), hit=STRONG)
        given_up(c, w)
        return w, c

    def test_a_block_beside_a_pack_is_neither_walked_to_nor_opened(self):
        from tests.test_way_tries import CHOP, play

        w, c = self.clearing(pack_beside_the_tree=True)
        reach = {p for h in w.entities for p in [(h.pos[0] + dx, h.pos[1] + dy) for dx in range(-BAR, BAR + 1) for dy in range(-BAR, BAR + 1)]}
        outs = play(w, c, 3)
        sent = [i for o in outs for i in o.intents or []]
        self.assertNotIn(CHOP, sent)
        self.assertFalse(any(i.get("verb") == "SetPosition" and (i["x"], i["y"]) in reach for i in sent), sent)
        self.assertEqual(len(asks(c)), 1, [o.reason for o in outs])

    def test_with_no_pack_the_block_is_opened(self):
        from tests.test_way_tries import CHOP, play

        w, c = self.clearing(pack_beside_the_tree=False)
        sent = [i for o in play(w, c, 8) for i in o.intents or []]
        self.assertIn(CHOP, sent)


class EveryOpWalkHoldsTest(unittest.TestCase):
    """Each walk family an op executor runs holds on a strong pack across the
    only way to its target: the op asks once and sends no step into the reach."""

    ROWS = ["#" * 23, "#" + "." * 20 + "T#", "#" * 23]  # a corridor, a tree at its east end
    GLYPHS = {**GLYPHS, "T": "tree"}

    def corridor(self) -> WorldModel:
        w = WorldModel(character_id=1, map_id=7, pos=(1, 1), perception=25)
        for y, row in enumerate(self.ROWS):
            for x, ch in enumerate(row):
                w.view.tiles[(x, y)] = self.GLYPHS[ch]
        w.terrain_center, w.terrain_map = (1, 1), 7
        w.alive, w.health, w.max_health, w.lives, w.gems = True, 10, 10, 5, 20
        return w

    def held(self, w: WorldModel, op: dict, *others: Entity) -> PlayContext:
        hostiles = pack(w, (11, 1), (12, 1), hit=STRONG)
        w.entities = [*hostiles, *others]
        c = ctx(op)
        reach = {(x, y) for h in hostiles for x in range(h.pos[0] - BAR, h.pos[0] + BAR + 1) for y in range(0, 3)}
        sent = []
        for _ in range(3):
            out = dispatch(w, c)
            sent += out.intents or []
            w.tick += 10
        steps = [(i["x"], i["y"]) for i in sent if i.get("verb") == "SetPosition"]
        self.assertFalse([p for p in steps if p in reach], f"{out.state}: {out.reason}")
        self.assertFalse([i for i in sent if i.get("verb") in ("Use", "Take", "Read")], sent)
        self.assertEqual(len(asks(c)), 1, out.reason)
        return c

    def test_loot_fetch_item(self):
        w = self.corridor()
        self.held(w, {"op": "fetch_item", "code": "small_potion"}, Entity("supply", 50, (20, 1), code="small_potion"))

    def test_investigate_read(self):
        w = self.corridor()
        w.perception = 8  # the sign at the far end is out of sight from here
        self.held(w, {"op": "read", "x": 21, "y": 1})

    def test_shop_buy_in_sight(self):
        w = self.corridor()
        self.held(w, {"op": "buy", "code": "small_potion"}, Entity("supply", 60, (20, 1), code="small_potion", gem_price=5))

    def test_solve_use_block(self):
        w = self.corridor()
        w.held_supplies = [InventorySupply(5, "matches")]
        self.held(w, {"op": "use_block", "x": 21, "y": 1, "code": "matches"})

    def test_break_block(self):
        w = self.corridor()
        w.held_supplies = [InventorySupply(4, "bronze_sword")]
        w.armed_code, w.armed_id = "bronze_sword", 4
        self.held(w, {"op": "break_block", "x": 21, "y": 1, "capability": "chop"})


class PrerequisiteWalkHoldsTest(unittest.TestCase):
    def test_the_walk_to_an_entrances_key_holds_on_a_pack_and_keeps_the_stop(self):
        """Level's walk to the item a locked entrance needs keeps out of the
        reach as the entrance walk does: no step into it, and the stop is held,
        not given up, while the entrance walk goes on."""
        from tests.test_commit_target import MAP, ctx as level_ctx, field
        from agentrealm_agent.knowledge_base import KnowledgeBase
        from agentrealm_agent.states.level import ENTRANCE_GOAL
        from agentrealm_agent.travel.knowledge import entrance_key

        kb = KnowledgeBase("sandbox")
        kb.entrances[entrance_key(MAP, (18, 4))] = {"map_id": MAP, "x": 18, "y": 4, "locked": True, "needs": "key"}
        w = field(at=(10, 4))
        w.view.tiles[(18, 4)] = "framed_door"
        hostiles = pack(w, (6, 2), (6, 6), hit=STRONG)  # across every row between us and the key
        w.entities = [*hostiles, Entity("supply", 51, (1, 4), "key")]
        m = Memory()
        plan = Plan([{"op": "enter_level", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        out = dispatch(w, level_ctx(m, plan, kb))
        self.assertEqual(out.state, "Level", out.reason)
        step = (out.intents[0]["x"], out.intents[0]["y"])
        self.assertGreater(step[0], 10, "toward the entrance, not into the pack's reach")
        self.assertEqual([s.pos for s in m.targets[ENTRANCE_GOAL].stops], [(1, 4)], "the stop is kept")


class PackIsWeighedWholeTest(unittest.TestCase):
    def setUp(self):
        self.w = world(["." * 20] * 11, at=(5, 5))

    def test_the_estimate_sums_every_hostile_that_can_reach_us(self):
        w = self.w
        # One in range; one 4 off; one chained to it 2 further, 6 off; one past
        # the reach radius but 2 from the last: the pack counts whole.
        hostiles = pack(w, (7, 5), (9, 5), (11, 5), (13, 5))
        e = decide(w, POLICY, dict(PARAM_DEFAULTS), frozenset({("npc", 200)}), False)
        whole = win_ratio(w.health, hostiles, w.threat, w.armed_code, worn_defense(w))
        self.assertAlmostEqual(e.ratio, whole)
        alone = win_ratio(w.health, hostiles[:1], w.threat, w.armed_code, worn_defense(w))
        self.assertLess(e.ratio, alone)

    def test_a_pack_the_estimate_cannot_beat_is_not_closed_on_one_at_a_time(self):
        w = self.w
        params = dict(PARAM_DEFAULTS)
        pack(w, (7, 5))
        self.assertTrue(decide(w, POLICY, params, frozenset({("npc", 200)}), False).fight, "one alone: fight")
        pack(w, (7, 5), (9, 4), (9, 6))
        e = decide(w, POLICY, params, frozenset({("npc", 200)}), False)
        self.assertFalse(e.fight, f"the pack of three: ratio {e.ratio:.2f}")

    def test_a_hostile_far_off_and_apart_from_the_pack_is_not_counted(self):
        w = self.w
        hostiles = pack(w, (7, 5), (17, 5))
        e = decide(w, POLICY, dict(PARAM_DEFAULTS), frozenset({("npc", 200)}), False)
        self.assertAlmostEqual(e.ratio, win_ratio(w.health, hostiles[:1], w.threat, w.armed_code, worn_defense(w)))


class WalkUnderAttackDoesNotWaitTest(unittest.TestCase):
    # The held queue's Step right may have run: we stand on (5, 5) or (6, 5).
    # A Step up lands on (5, 4) from the one but the wall (6, 4) from the other.
    ROWS = [
        "..........",
        "..........",
        "..........",
        "..........",
        "......#...",
        "..........",
        "..........",
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "T.directives.toml").write_text("")

    def runner(self, attacked: bool) -> Runner:
        cfg = CharacterConfig("T", "sandbox", POLICY, self.dir / "T.toml")
        r = Runner(cfg, FakeClient([]), 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = world(self.ROWS, at=(5, 5))
        [hitter] = pack(w, (7, 5))
        if attacked:
            w.attacked_tick, w.attacker = w.tick, (hitter.kind, hitter.id)
        m = Memory(need_self=False, need_position=False)
        m.pending_intents, m.pending_next_index, m.queue_sent_tick = [{"verb": "Step", "direction": "right"}], 0, w.tick
        r.world, r.mem = w, m
        r.plan = Plan([], dict(PARAM_DEFAULTS))
        return r

    def retreat(self) -> tuple[list[dict], Decision]:
        intents = [{"verb": "Step", "direction": "up"}, {"verb": "Step", "direction": "up"}]
        return intents, Decision(intents[0], "retreat to safe ground", True, state="Retreat")

    def test_the_doubt_stands_for_the_walk_as_planned(self):
        r = self.runner(attacked=True)
        intents, _ = self.retreat()
        starts = possible_positions(r.world, r.mem)
        self.assertEqual(starts, [(5, 5), (6, 5)])
        self.assertEqual(unconfirmed_send(r.world, r.mem, POLICY, None, intents, starts), STEP_DOUBT)

    def test_under_attack_it_takes_the_best_open_step(self):
        r = self.runner(attacked=True)
        intents, d = self.retreat()
        starts = possible_positions(r.world, r.mem)
        got, sent, doubt = r.move_under_attack(d, intents, starts)
        self.assertIsNone(doubt)
        [one] = sent
        self.assertEqual(one["verb"], "Step")
        for pos in starts:
            self.assertTrue(r.world.view.walkable(step_landing(pos, one["direction"])), pos)
        self.assertGreaterEqual(chebyshev(step_landing((5, 5), one["direction"]), (7, 5)), 2, "never nearer the hitter")
        self.assertIn("best open step", got.reason)
        self.assertEqual(got.state, "Retreat")

    def cornered(self, tick_hz: int = 10) -> tuple[Runner, Decision, list[dict], str | None]:
        """Walled in but for the cell the held Step may have taken us to, and
        the hitter's beside us: no Step lands open from both cells we may stand on."""
        r = self.runner(attacked=True)
        r.plan.tick_hz = tick_hz
        w = r.world
        w.view.tiles.update({p: "wall" for p in [(4, 4), (4, 5), (4, 6), (5, 4), (5, 6), (6, 6), (7, 5)]})
        w.entities[0].pos = (6, 4)
        w.view.tiles[(6, 4)] = "dirt"
        intents, d = self.retreat()
        starts = possible_positions(w, r.mem)
        self.assertIsNone(open_step(w, r.mem, POLICY, None, intents, starts, [(6, 4)]))
        return (r, *r.move_under_attack(d, intents, starts))

    def test_with_no_open_step_it_hands_to_flee(self):
        _, got, sent, doubt = self.cornered()
        self.assertIsNone(doubt)
        self.assertEqual(got.state, "Flee")
        self.assertIn("no open step, cornered", got.reason)
        self.assertTrue(any(i.get("verb") == "Use" for i in sent), sent)

    def test_the_cornered_move_is_paced_at_the_worlds_tick_rate(self):
        """It runs in the plan's context, as every decision does: every Use is
        paced at the world's tick rate (A99), not the default."""
        from agentrealm_agent import brain

        seen = []
        real = brain.cornered

        def spy(w, ctx, hostiles):
            seen.append((ctx.tick_hz, ctx.plan))
            return real(w, ctx, hostiles)

        with mock.patch.object(brain, "cornered", spy):
            r, *_ = self.cornered(tick_hz=20)
        self.assertEqual(seen, [(20, r.plan)])

    def test_while_parking_the_cornered_move_runs_with_no_op(self):
        r = self.runner(attacked=True)
        r.mem.parking = True
        self.assertIsNone(r._play_args(r.plan)["plan"])
        r.mem.parking = False
        self.assertIs(r._play_args(r.plan)["plan"], r.plan)

    def tick(self, attacked: bool) -> list[dict] | None:
        """One runner tick in which Retreat's walk replaces the held queue: what is sent."""
        r = self.runner(attacked)
        r.client = FakeClient([{"tick": r.world.tick}])
        r.mem.held_queue = {"queue_id": "q0", "next_index": 0}
        _, d = self.retreat()
        with mock.patch.object(Runner, "reflex_while_held", return_value=d), mock.patch.object(
            Runner, "intents_for", return_value=self.retreat()[0]
        ):
            r.tick()
        return r.client.sent[0][0]

    def test_a_tick_under_attack_sends_a_step_not_a_wait(self):
        sent = self.tick(attacked=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["verb"], "Step")

    def test_a_tick_not_under_attack_still_stops_to_reread_position(self):
        self.assertEqual(self.tick(attacked=False), [{"verb": "Wait"}])


if __name__ == "__main__":
    unittest.main()
