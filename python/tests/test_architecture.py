"""AI plans, state machine executes (PLAN.md **Architecture**).

Reflexes act on what is happening now; executors run only for the plan's
top op; with no plan op the safe default explores safe ground. So exactly
one thing picks the movement target, states never take turns moving the
character (the A58 runs 3–7 pacing), and a decision is never idle.
"""

from __future__ import annotations

import random
import re
import unittest
from pathlib import Path

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan
from agentrealm_agent.states import PlayContext, StateOutcome, dispatch
from agentrealm_agent.world import Entity, WorldModel

# Spelled out here, not imported, so the test reads the spec rather than the code.
REFLEXES = {"Sync", "Downed", "Escape", "Retreat", "Heal", "Fight", "Flee", "Pickup", "Recover", "Detour", "Greet"}
SAFE_DEFAULT = "Explore"


def field_world(width: int = 21, height: int = 9, at=(2, 4), fog: bool = True) -> WorldModel:
    """Open dirt. With ``fog`` the outer ring is unknown, so there is frontier to explore;
    without it the field is walled in and fully known."""
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=8)
    for x in range(width):
        for y in range(height):
            edge = x in (0, width - 1) or y in (0, height - 1)
            if edge and fog:
                continue
            w.view.tiles[(x, y)] = "wall" if edge else "dirt"
    w.terrain_center, w.terrain_map = at, 7
    w.health, w.max_health = 100, 100
    return w


def tempt(w: WorldModel) -> None:
    """Everything a state used to start on its own: a worthwhile pickup off the
    route (Loot), a readable sign (Investigate), and an NPC never spoken to (Say)."""
    w.entities = [
        Entity("supply", 31, (4, 7), "heart"),
        Entity("npc", 41, (12, 1), "villager"),
    ]
    w.view.readable[(3, 1)] = True


def play(w: WorldModel, plan: Plan | None, decisions: int = 30, **policy_kw) -> list[StateOutcome]:
    """Decide and apply each move and take, the way the runner applies them.
    Each outcome's ``at`` is where the character stood after it."""
    m = Memory()
    ctx = PlayContext(
        m, Policy(kind="scripted", goals=[], **policy_kw), random.Random(3), plan=plan, directives=default_directives()
    )
    out: list[StateOutcome] = []
    for _ in range(decisions):
        o = dispatch(w, ctx)
        out.append(o)
        for i in o.intents or []:
            if i.get("verb") == "SetPosition":
                w.pos = (i["x"], i["y"])
                if m.path and m.path[0] == w.pos:
                    m.path = m.path[1:]
                nav_stuck.on_step(m, w)
            elif i.get("verb") == "Take":
                w.entities = [e for e in w.entities if not (e.kind == "supply" and e.id == i["supply_id"])]
        o.at = w.pos
        w.tick += 4
    return out


def movers(outcomes: list[StateOutcome]) -> list[str]:
    return [o.state for o in outcomes if any(i.get("verb") == "SetPosition" for i in o.intents or [])]


def travel(x: int, y: int) -> Plan:
    return Plan([{"op": "travel", "to": "point", "x": x, "y": y}], dict(PARAM_DEFAULTS))


class NoPlanTest(unittest.TestCase):
    def test_only_reflexes_and_the_safe_default_move(self):
        w = field_world()
        tempt(w)
        outs = play(w, None)
        acting = {o.state for o in outs if o.intents}
        self.assertTrue(acting, "something must act")
        self.assertLessEqual(acting, REFLEXES | {SAFE_DEFAULT}, acting)
        # Nothing was read on the agent's own initiative, and the only Say is
        # Greet's one-tick hello to an NPC in sight (A65): no walk to talk.
        verbs = {i["verb"] for o in outs for i in o.intents or []}
        self.assertNotIn("Read", verbs)
        self.assertTrue(all(o.state == "Greet" for o in outs for i in o.intents or [] if i["verb"] == "Say"))

    def test_nothing_to_do_is_never_idle(self):
        # Walled in, fully known: no frontier, no plan, nothing in sight.
        w = field_world(width=7, height=7, at=(3, 3), fog=False)
        for o in play(w, None, decisions=12):
            self.assertTrue(o.intents, f"idle decision: {o.state}: {o.reason}")

    def test_an_empty_plan_is_no_plan(self):
        w = field_world(width=7, height=7, at=(3, 3), fog=False)
        for o in play(w, Plan([], dict(PARAM_DEFAULTS)), decisions=6):
            self.assertTrue(o.intents, f"idle decision: {o.state}: {o.reason}")
            self.assertEqual(o.state, SAFE_DEFAULT)


class PlanOpTest(unittest.TestCase):
    def test_only_the_travel_executor_moves(self):
        w = field_world()
        tempt(w)
        outs = play(w, travel(18, 4))
        self.assertIn((18, 4), [o.at for o in outs], [o.reason for o in outs])
        arrived = [o.at for o in outs].index((18, 4))
        self.assertEqual(set(movers(outs[: arrived + 1])), {"Travel"})
        # Arrived, the stack is empty: the safe default has the move.
        self.assertEqual(set(movers(outs[arrived + 1 :])), {SAFE_DEFAULT})

    def test_only_the_explore_executor_moves_for_explore_area(self):
        w = field_world()
        tempt(w)
        plan = Plan([{"op": "explore_area", "x": 10, "y": 4, "radius": 30}], dict(PARAM_DEFAULTS))
        outs = play(w, plan, decisions=20)
        self.assertEqual(set(movers(outs)), {"Explore"})
        self.assertTrue(all(o.state == "Explore" for o in outs if o.intents), [o.state for o in outs])

    def test_a_plan_op_its_executor_carries_out(self):
        # The sign is read because the plan says so, not because it is in sight.
        w = field_world()
        tempt(w)
        plan = Plan([{"op": "read", "x": 3, "y": 1}], dict(PARAM_DEFAULTS))
        out = play(w, plan, decisions=1)[0]
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents[0]["verb"], "Read")

    def test_a_reflex_still_outranks_the_executor(self):
        w = field_world()
        w.entities = [Entity("supply", 31, (3, 4), "heart")]  # adjacent, worthwhile
        out = play(w, travel(18, 4), decisions=1)[0]
        self.assertEqual(out.state, "Pickup")
        self.assertEqual(out.intents[0]["verb"], "Take")

    def test_an_op_its_executor_cannot_act_on_stalls_while_the_safe_default_moves(self):
        # Nothing priced in sight: Shop has nothing to do. The decision is
        # not idle, and the op is dropped once it has stalled long enough.
        w = field_world()
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        plan.tick_hz = 1  # PLAN_STALL_SECONDS ticks
        outs = play(w, plan, decisions=12)
        for o in outs:
            self.assertTrue(o.intents, f"idle decision: {o.state}: {o.reason}")
            self.assertEqual(o.state, SAFE_DEFAULT)
        self.assertIsNone(plan.current())

    def test_a_permanent_occupant_drops_the_travel_op_by_the_stall_bound(self):
        # Travel holds while an NPC stands on its route's first step, but the
        # hold is not progress: the op is dropped once it has stalled for
        # PLAN_STALL_SECONDS, then the safe default has the move.
        w = field_world(width=9, height=3, at=(1, 1), fog=False)
        w.entities = [Entity("npc", 61, (2, 1), "villager")]
        plan = travel(6, 1)
        plan.tick_hz = 1
        outs = play(w, plan, decisions=12, hostile=["character"])
        self.assertTrue(outs[0].wait and outs[0].state == "Travel", outs[0].reason)
        held = [o for o in outs if o.state == "Travel"]
        self.assertTrue(all(o.wait and not o.intents for o in held))
        self.assertIsNone(plan.current(), "dropped")
        self.assertLessEqual((len(held) - 1) * 4, PLAN_STALL_SECONDS + 4)  # 4 ticks a decision

    def test_wait_says_why_and_holds_the_round(self):
        w = field_world()
        plan = Plan([{"op": "wait", "seconds": 2, "why": "let the door cycle"}], dict(PARAM_DEFAULTS))
        out = play(w, plan, decisions=1)[0]
        self.assertTrue(out.wait)
        self.assertIsNone(out.intents)
        self.assertIn("let the door cycle", out.reason)


class ReflexBoundsTest(unittest.TestCase):
    def test_heal_takes_only_close_food(self):
        # Mid health, no threat: food far off the line is a planner's errand, not Heal's.
        w = field_world()
        w.health = 50
        w.entities = [Entity("supply", 51, (12, 7), "apple")]
        outs = play(w, travel(18, 4), decisions=6)
        self.assertNotIn("Heal", movers(outs))
        w = field_world()
        w.health = 50
        w.entities = [Entity("supply", 51, (4, 5), "apple")]
        self.assertEqual(play(w, travel(18, 4), decisions=1)[0].state, "Heal")

    def test_escape_never_targets_a_hazard(self):
        w = WorldModel(character_id=1, map_id=7, pos=(1, 1), perception=3)
        for y in range(3):
            for x in range(4):
                w.view.tiles[(x, y)] = "dirt" if x == 3 else "lava"
        out = play(w, None, decisions=1)[0]
        self.assertEqual(out.state, "Escape")
        self.assertIn("(3, ", out.reason)

    def test_a_boxed_in_explore_op_still_stalls(self):
        w = field_world(width=3, height=3, at=(1, 1), fog=False)
        plan = Plan([{"op": "explore_area", "x": 9, "y": 9, "radius": 1}], dict(PARAM_DEFAULTS))
        plan.tick_hz = 1
        outs = play(w, plan, decisions=12)
        self.assertTrue(all(o.wait and "boxed in" in o.reason for o in outs), [o.reason for o in outs])
        self.assertIsNone(plan.current())


class A58PacingPairsTest(unittest.TestCase):
    """docs/observations/A58_live_play.md runs 3–7: two states, two targets,
    taking turns moving the character (the goto walk against Break, Equip,
    Loot and Explore). With one mover per decision the pairs cannot form."""

    def test_no_two_states_take_turns_on_a_walk(self):
        w = field_world(width=31)
        tempt(w)
        w.entities.append(Entity("supply", 32, (9, 1), "heart"))
        outs = play(w, travel(28, 4), decisions=40)
        arrived = [o.at for o in outs].index((28, 4))
        moved = movers(outs[: arrived + 1])
        switches = sum(1 for a, b in zip(moved, moved[1:]) if a != b)
        self.assertEqual(switches, 0, moved)
        self.assertFalse(any("oscillation" in y for o in outs[: arrived + 1] for y in o.yielded))

    def test_a_reached_goto_does_not_pull_back_from_explore(self):
        # Runs 7/8: once the goto is reached the plan pops it; the walk away is not undone.
        w = field_world()
        plan = Plan(
            [
                {"op": "travel", "to": "point", "x": 6, "y": 4},
                {"op": "explore_area", "x": 0, "y": 0, "radius": 1 << 30},
            ],
            dict(PARAM_DEFAULTS),
        )
        outs = play(w, plan, decisions=40)
        moved = movers(outs)
        first_explore = moved.index("Explore")
        self.assertEqual(set(moved[:first_explore]), {"Travel"})
        self.assertEqual(set(moved[first_explore:]), {"Explore"}, moved)


# A87: rules, not run patches. Comments in agent source state the rule in game
# terms; the run that found it is cited in PLAN.md and docs/observations. This
# count only falls: lower it in the PR that removes the citations.
RUN_CITATION_CEILING = 114
# ``run 4``, ``Run 4``, ``runs 3, 4``: any form that names a live run.
RUN_CITATION = re.compile(r"\bruns? \d+\b", re.IGNORECASE)
PYTHON_DIR = Path(__file__).resolve().parents[1]
AGENT_SOURCE = [*(PYTHON_DIR / "agentrealm_agent").rglob("*.py"), PYTHON_DIR / "starter_agent.py"]


class RulesNotRunPatchesTest(unittest.TestCase):
    def test_run_citations_in_agent_source_only_fall(self):
        hits = sorted(
            (str(f.relative_to(PYTHON_DIR)), n)
            for f in AGENT_SOURCE
            if (n := sum(1 for line in f.read_text().splitlines() if RUN_CITATION.search(line)))
        )
        total = sum(n for _, n in hits)
        self.assertLessEqual(
            total,
            RUN_CITATION_CEILING,
            "A87: a fix from a live run generalizes a rule or goes to the planner; cite the run in "
            f"PLAN.md and docs/observations, not in code. Citations by file: {hits}",
        )
        if total < RUN_CITATION_CEILING:
            self.fail(f"A87: {total} run citations; lower RUN_CITATION_CEILING to match.")


if __name__ == "__main__":
    unittest.main()
