"""A58 run 7 offline: the two shapes that burned the live hour (A10, A15, A16).

1. The ``goto`` reached the two-level planner's last corridor waypoint short of
   its target, the target turned out to have no path, and the next decision
   was ``explore``: the goto was neither reached nor given up.
2. Heal walked to food it could not reach and paced between two cells next to
   it for the rest of the hour; every oscillation event gave nothing up.

Both are rebuilt here through the real dispatcher and planner. The node
budget is patched low so a short test map takes the same budget-hit path
(corridor and window search) as the live 150-block walk.
"""

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import cost_path, planner
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel, ZoneFact

SMALL_BUDGET = 30


def ctx(policy: Policy) -> PlayContext:
    """The built-in plan for ``policy``: a goto is a ``travel`` point op."""
    return PlayContext(
        Memory(),
        policy,
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        knowledge=KnowledgeBase.empty("sandbox"),
        plan=Plan.from_policy(policy, dict(PARAM_DEFAULTS)),
    )


WALK = "travel:point"  # the goal label of a goto's travel op walk


def goto_backed_off(w: WorldModel, c: PlayContext) -> bool:
    return nav_stuck.backed_off(c.memory, WALK, w.map_id, GOTO, w.tick)


def land(w: WorldModel, c: PlayContext, out) -> None:
    """The decision's SetPosition lands; the runner trims the path and counts the move."""
    w.tick += 10
    if out.intents and out.intents[0]["verb"] == "SetPosition":
        w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        if c.memory.path[:1] == [w.pos]:
            c.memory.path = c.memory.path[1:]
        nav_stuck.on_step(c.memory, w)


# --- 1. goto dropped at a corridor waypoint -------------------------------

GOTO = (40, 0)


def strip_world() -> WorldModel:
    """Map 1: open dirt seven rows high, seen only as far as perception reaches.
    The goto cell is water, which a terrain read shows once it is in sight."""
    w = WorldModel(character_id=1, map_id=1, pos=(0, 0), perception=5, health=10, max_health=10)
    for x in range(-20, 6):
        for y in range(-3, 4):
            w.view.tiles[(x, y)] = "dirt"
    read_terrain(w)
    return w


def read_terrain(w: WorldModel) -> None:
    x0, y0, width, height = w.perception_rect()
    for x in range(x0, x0 + width):
        for y in range(y0, y0 + height):
            if (x, y) not in w.view.tiles:
                w.view.tiles[(x, y)] = "water" if (x, y) == GOTO or abs(y) > 3 else "dirt"
    w.terrain_center, w.terrain_map = w.pos, 1


class GotoAtCorridorWaypointTest(unittest.TestCase):
    def test_goto_stays_owed_until_stuck_detection_gives_it_up(self):
        w = strip_world()
        c = ctx(Policy(kind="scripted", goals=["goto", "explore"], goto=GOTO))
        reasons = []
        with mock.patch.object(planner, "FINE_NODE_BUDGET", SMALL_BUDGET):
            while not c.memory.nav_stuck.stuck_signals and w.tick < 2000:
                out = dispatch(w, c)
                reasons.append(out.reason)
                land(w, c, out)
                read_terrain(w)
        waypoints = {r for r in reasons if r.startswith(f"{WALK} → (") and "[" not in r}
        self.assertGreater(len(waypoints), 2, "walked waypoint to waypoint, as in the live trace")
        self.assertTrue(all(r.startswith(WALK) for r in reasons[:-1]), "the travel op kept the move")
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual([(s["goal"], s["target"]) for s in signals], [(WALK, list(GOTO))])
        self.assertTrue(signals[0]["reason"], "given up with a reason, as the A16 give-up rule needs")
        self.assertTrue(goto_backed_off(w, c), "backed off")


# --- 2. Heal (and Loot) walking to food it cannot reach -------------------

FOOD = (400, 611)


def pond_world(health=5) -> WorldModel:
    """A walled field of dirt, nothing left to explore, with the food on a
    grass cell in the middle of a pond."""
    w = WorldModel(character_id=1, map_id=1, pos=(404, 605), perception=8, health=health, max_health=10)
    for x in range(380, 430):
        for y in range(590, 630):
            edge = x in (380, 429) or y in (590, 629)
            w.view.tiles[(x, y)] = "stone" if edge else "dirt"
    for x in range(398, 403):
        for y in range(609, 614):
            w.view.tiles[(x, y)] = "water"
    w.view.tiles[FOOD] = "grass"
    w.terrain_center, w.terrain_map = w.pos, 1
    w.entities = [Entity("supply", 7, FOOD, "apple")]
    return w


def drain(w: WorldModel) -> None:
    """The pond dried up: the food is reachable."""
    for p, block in list(w.view.tiles.items()):
        if block in ("water", "grass"):
            w.view.tiles[p] = "dirt"


def play(w: WorldModel, c: PlayContext, decisions: int) -> list:
    outs = []
    with mock.patch.object(planner, "FINE_NODE_BUDGET", SMALL_BUDGET):
        for _ in range(decisions):
            out = dispatch(w, c)
            outs.append(out)
            land(w, c, out)
    return outs


def fetch_ctx() -> PlayContext:
    """Loot walks to a supply only for the plan's ``fetch_item`` op."""
    c = ctx(Policy(kind="scripted", goals=["explore"]))
    c.plan = Plan([{"op": "fetch_item", "code": "apple"}], dict(PARAM_DEFAULTS))
    return c


class UnreachableFoodTest(unittest.TestCase):
    def test_heal_gives_the_food_up_instead_of_pacing(self):
        w, c = pond_world(), ctx(Policy(kind="scripted", goals=["explore"], pickup=False))
        w.pos = (403, 608)  # within Heal's FOOD_REACH of the food
        outs = play(w, c, 80)
        signals = c.memory.nav_stuck.stuck_signals
        # Given up at the dead end, retried once when the 300-tick backoff ends,
        # given up again (now backed off 600). In between the safe default
        # looks around, so the retry walks to the dead end again.
        self.assertEqual([(s["goal"], s["target"], s["reason"]) for s in signals], [("heal_food", list(FOOD), "no_path")] * 2)
        self.assertEqual(c.memory.nav_stuck.oscillations, [], "no pacing at all")
        heal_walk = [o.reason.startswith("heal_food") for o in outs]
        self.assertEqual(sum(heal_walk), 6, "walked until the dead end, twice")
        self.assertEqual(heal_walk[:4], [True, True, True, False], "given up at the dead end")

    def test_loot_gives_the_pickup_up_instead_of_pacing(self):
        w = pond_world(health=10)
        c = fetch_ctx()
        play(w, c, 80)
        signals = c.memory.nav_stuck.stuck_signals
        self.assertIn(("loot", list(FOOD)), [(s["goal"], s["target"]) for s in signals])
        self.assertEqual(c.memory.nav_stuck.oscillations, [])

    def test_the_planner_has_no_step_off_the_dead_end(self):
        w = pond_world()
        w.pos = (401, 608)  # the closest cell to the food the window search reaches
        with mock.patch.object(planner, "FINE_NODE_BUDGET", SMALL_BUDGET):
            self.assertIsNone(cost_path(w, FOOD))
            w.pos = (403, 606)
            self.assertTrue(cost_path(w, FOOD), "still a step while one gets closer")


def bounded_step(*args):
    # Imported here so the run 7 shapes above fail on main by assertion, not import.
    from agentrealm_agent.pathing import bounded_step

    return bounded_step(*args)


class HealWalkBoundedTest(unittest.TestCase):
    """Whatever makes a Heal walk go nowhere, its own bound ends it."""

    def test_heal_only_pacing_is_given_up_by_the_guard(self):
        # A path that steps to the other cell every decision, as run 7's did.
        w, c = pond_world(), ctx(Policy(kind="scripted", goals=["explore"], pickup=False))
        w.pos = (403, 608)  # both cells within Heal's FOOD_REACH of the food
        flip = {(403, 608): (402, 608), (402, 608): (403, 608)}
        with mock.patch("agentrealm_agent.states.heal.cost_path", lambda w, *a, **k: [flip[w.pos]]):
            outs = play(w, c, 20)
        events = c.memory.nav_stuck.oscillations
        self.assertEqual([(e.get("goal"), e["states"]) for e in events], [("heal_food", ["Heal"])])
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual([(s["goal"], s["reason"]) for s in signals][:1], [("heal_food", "pacing")])
        self.assertFalse(any(o.reason.startswith("heal_food") for o in outs[6:]), "food backed off")

    def test_a_walk_with_no_progress_in_its_window_is_given_up(self):
        w, m = pond_world(), Memory()
        path = [(403, 606), (402, 607)]
        self.assertEqual(bounded_step(m, w, "heal_food", FOOD, set(), lambda: path), (403, 606))
        att = nav_stuck.active(m, w)
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: path))
        self.assertEqual(m.nav_stuck.stuck_signals[-1]["reason"], "time")
        self.assertTrue(nav_stuck.backed_off(m, "heal_food", 1, FOOD, w.tick))
        self.assertIsNone(nav_stuck.active(m, w))
        self.assertIsNot(att, nav_stuck.active(m, w))

    def test_progress_keeps_the_walk(self):
        w, m = pond_world(), Memory()
        bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606), (402, 607)])
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        w.pos = (403, 606)
        self.assertEqual(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(402, 607)]), (402, 607))
        self.assertEqual(m.nav_stuck.stuck_signals, [])

    def test_no_path_gives_up_and_the_backoff_skips_planning(self):
        w, m = pond_world(), Memory()
        self.assertIsNone(bounded_step(m, w, "loot", FOOD, set(), lambda: None))
        self.assertEqual(m.nav_stuck.stuck_signals[-1]["reason"], "no_path")
        planned = []
        self.assertIsNone(bounded_step(m, w, "loot", FOOD, set(), lambda: planned.append(1) or [(403, 606)]))
        self.assertEqual(planned, [], "backed off: not planned again")
        w.tick += nav_stuck.BACKOFF_BASE_TICKS
        self.assertEqual(bounded_step(m, w, "loot", FOOD, set(), lambda: [(403, 606)]), (403, 606))

    def test_a_taken_first_step_waits_out_its_window(self):
        w, m = pond_world(), Memory()
        w.entities.append(Entity("npc", 3, (403, 606), "villager"))
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]))
        self.assertEqual(m.nav_stuck.stuck_signals, [], "an occupant moves on")
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]))
        self.assertEqual(m.nav_stuck.stuck_signals[-1]["reason"], "time")

    def test_a_walk_with_no_move_leaves_the_active_walk_and_its_window_alone(self):
        # Loot's guard runs every decision; a pickup it cannot step toward must
        # not restart the window of the walk that is moving (review on #101).
        w, m = pond_world(), Memory()
        walking = nav_stuck.track(m, w, "explore", (420, 600))
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        w.entities.append(Entity("npc", 3, (403, 606), "villager"))
        self.assertIsNone(bounded_step(m, w, "loot", FOOD, set(), lambda: [(403, 606)]))
        self.assertIs(nav_stuck.active(m, w), walking)
        self.assertEqual(nav_stuck.stuck_reason(walking, w.tick), "time")

    def test_food_and_an_owed_goto_both_behind_fog_do_not_hold_the_goto_window(self):
        """Review on #101: the food's first step and the goto's are both unseen.

        Heal tries the food each decision and has no step; the plan's travel
        op (the goto) has none either. The food must not restart the goto's
        window, so the goto escalates once ``PROGRESS_TICK_LIMIT`` has
        passed, and the food walk is bounded on its own (given up on time).
        """
        # A dead-end pocket whose only way out, east, is an unseen cell.
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0), perception=5, health=5, max_health=10)
        for x in range(-3, 1):
            w.view.tiles[(x, 0)] = "dirt"
            w.view.tiles[(x, 1)] = w.view.tiles[(x, -1)] = "stone"
        for p in ((-4, 0), (1, 1), (1, -1)):
            w.view.tiles[p] = "stone"
        w.terrain_center, w.terrain_map = w.pos, 1
        w.entities = [Entity("supply", 7, (3, 0), "apple")]
        c = ctx(Policy(kind="scripted", goals=["goto", "explore"], goto=(5, 0), pickup=False))
        for _ in range(nav_stuck.PROGRESS_TICK_LIMIT // 10 + 1):
            dispatch(w, c)
            w.tick += 10
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual([(s["goal"], s["reason"]) for s in signals], [("heal_food", "time")])
        goto = nav_stuck.active(c.memory, w)
        self.assertEqual(goto.goal, WALK)
        self.assertGreaterEqual(goto.level, nav_stuck.CAUTIOUS, "the goto's window ran out")

    def test_a_blocked_step_after_an_old_walk_is_not_judged_on_the_old_window(self):
        # Review on #101: walk a step, come back much later, find the way taken.
        w, m = pond_world(), Memory()
        self.assertEqual(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]), (403, 606))
        w.tick += 1000
        w.entities.append(Entity("npc", 3, (403, 606), "villager"))
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]))
        self.assertEqual(m.nav_stuck.stuck_signals, [], "the wait has only just started")

    def test_a_wait_left_and_taken_up_again_starts_over(self):
        # Review on #101, path 1: blocked, Heal leaves, comes back still blocked.
        w, m = pond_world(), Memory()
        w.entities.append(Entity("npc", 3, (403, 606), "villager"))
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]))
        m.nav_stuck.decision += 5
        w.tick += 1000
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]))
        self.assertEqual(m.nav_stuck.stuck_signals, [], "the wait has only just started again")
        for _ in range(nav_stuck.PROGRESS_TICK_LIMIT // 10):
            m.nav_stuck.decision += 1
            w.tick += 10
            bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)])
        self.assertEqual([s["reason"] for s in m.nav_stuck.stuck_signals], ["time"], "waited on consecutive decisions")

    def test_a_walk_left_and_taken_up_again_starts_a_fresh_window(self):
        # Review on #101, path 2: a step, a long pause, a step from one cell further back.
        w, m = pond_world(), Memory()
        self.assertEqual(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]), (403, 606))
        m.nav_stuck.decision += 5
        w.tick += 1000
        w.pos = (405, 604)
        self.assertEqual(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(404, 605)]), (404, 605))
        self.assertEqual(m.nav_stuck.stuck_signals, [])

    def test_a_step_ends_the_wait(self):
        w, m = pond_world(), Memory()
        villager = Entity("npc", 3, (403, 606), "villager")
        w.entities.append(villager)
        bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)])
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT - 10
        w.entities.remove(villager)
        self.assertEqual(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]), (403, 606))
        w.entities.append(villager)
        w.tick += 10
        self.assertIsNone(bounded_step(m, w, "heal_food", FOOD, set(), lambda: [(403, 606)]))
        self.assertEqual(m.nav_stuck.stuck_signals, [], "waiting again from now")

    def test_taking_the_food_ends_its_walk(self):
        w, c = pond_world(), ctx(Policy(kind="scripted", goals=["explore"], pickup=False))
        drain(w)
        w.pos = (403, 608)
        food_key = nav_stuck.goal_key("heal_food", 1, FOOD)
        self.assertEqual(dispatch(w, c).reason, f"heal_food → {FOOD}")
        self.assertIn(food_key, c.memory.nav_stuck.attempts)
        w.pos = (401, 610)
        self.assertEqual(dispatch(w, c).intents, [{"verb": "Take", "supply_id": 7}])
        self.assertNotIn(food_key, c.memory.nav_stuck.attempts, "a later walk there starts fresh")

    def test_a_pickup_in_reach_ends_its_loot_walk(self):
        w = pond_world(health=10)
        c = fetch_ctx()
        drain(w)
        w.pos = (403, 608)
        loot_key = nav_stuck.goal_key("loot", 1, FOOD)
        self.assertEqual(dispatch(w, c).reason, f"fetch apple → {FOOD}")
        self.assertIn(loot_key, c.memory.nav_stuck.attempts)
        w.pos = (401, 610)
        self.assertEqual(dispatch(w, c).intents, [{"verb": "Take", "supply_id": 7}])
        self.assertNotIn(loot_key, c.memory.nav_stuck.attempts)

    def test_heal_walks_to_the_next_safe_tile_once_one_is_given_up(self):
        w, c = pond_world(), ctx(Policy(kind="scripted", goals=["explore"]))
        w.entities = []
        w.zones[1] = {(404, 600): ZoneFact(safe=True), (410, 605): ZoneFact(safe=True), (404, 605): ZoneFact(safe=False)}
        out = dispatch(w, c)
        self.assertEqual(out.reason, "heal_measure → (404, 600)")
        nav_stuck.give_up(c.memory, w, nav_stuck.active(c.memory, w), "time")
        out = dispatch(w, c)
        self.assertEqual(out.reason, "heal_measure → (410, 605)")


if __name__ == "__main__":
    unittest.main()
