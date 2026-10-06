"""The idle watchdog (A61): the character never stands around doing nothing.

Live runs (docs/observations/A58_live_play.md) showed the character standing
still for minutes. These drive the real dispatcher through the cases the
watchdog must catch, and the one it must leave alone.
"""

import random
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, idle_watchdog
from agentrealm_agent.acceptance_survival import IDLE_GATE_SECONDS, SurvivalAcceptanceMetrics
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.healing import save_regen_yes
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import WorldModel, ZoneFact

LIMIT = idle_watchdog.redirect_ticks(10)  # 60 s at 10 ticks/s
EVERY = 10  # ticks between decisions in these runs


def corridor(at=(0, 0)) -> WorldModel:
    """Map 1: an east-west dirt corridor, walled north and south, fog at both ends."""
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=12, health=10, max_health=10)
    for x in range(-6, 30):
        w.view.tiles[(x, -1)] = w.view.tiles[(x, 1)] = "stone"
        w.view.tiles[(x, 0)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


def pen() -> WorldModel:
    """Map 7: a 3x3 dirt pen walled in stone, nothing left to explore; a safe zone at (1, 1)."""
    w = WorldModel(character_id=1, map_id=7, pos=(1, 1), perception=5, health=5, max_health=10)
    for x in range(-1, 4):
        for y in range(-1, 4):
            w.view.tiles[(x, y)] = "dirt" if 0 <= x <= 2 and 0 <= y <= 2 else "stone"
    w.terrain_center, w.terrain_map = (1, 1), 7
    w.record_respawn_anchor(7, (1, 1))
    w.zones[7] = {(1, 1): ZoneFact(safe=True)}
    return w


def ctx(policy: Policy, m: Memory | None = None, kb: KnowledgeBase | None = None, plan: Plan | None = None):
    return PlayContext(
        m or Memory(),
        policy,
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        knowledge=kb or KnowledgeBase.empty("sandbox"),
        plan=plan,
    )


def run_until(w: WorldModel, c: PlayContext, tick: int, *, move: bool = False):
    """Dispatch every ``EVERY`` ticks up to ``tick``; with ``move``, a sent step lands."""
    out = None
    while w.tick <= tick:
        out = dispatch(w, c)
        if move and out.intents and out.intents[0].get("verb") == "SetPosition":
            w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        w.tick += EVERY
    return out


class IdleWatchdogTest(unittest.TestCase):
    def test_heal_waiting_with_no_health_back_is_redirected(self):
        # Standing in the pen with nothing to do for 30 s, then Heal rests
        # with no health coming back. Heal's own bound counts from when it
        # began waiting (another 60 s); the watchdog counts from the last
        # productive tick, so the stretch never passes 60 s.
        w, kb = pen(), KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        m = Memory(heal_backoff_until=LIMIT // 2)
        c = ctx(Policy(kind="scripted", goals=["explore"]), m, kb)
        out = run_until(w, c, LIMIT - EVERY)
        self.assertEqual((out.state, out.reason), ("Heal", "rest in safe zone"))
        self.assertEqual(m.idle.events, [])
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Heal")
        [event] = m.idle.events
        self.assertEqual((event["event"], event["state"], event["cell"]), ("idle_redirect", "Heal", [1, 1]))
        self.assertEqual(event["ticks_idle"], LIMIT)
        self.assertTrue(idle_watchdog.held_off(m, "Heal", w.tick + 1))
        w.tick += nav_stuck.BACKOFF_BASE_TICKS - 1
        self.assertNotEqual(dispatch(w, c).state, "Heal", "backed off, not retried at once")

    def test_heal_with_health_rising_is_not_idle(self):
        w, kb = pen(), KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        m = Memory()
        c = ctx(Policy(kind="scripted", goals=["explore"]), m, kb)
        w.health, w.max_health = 1, 100
        while w.tick <= 3 * LIMIT:
            self.assertEqual(dispatch(w, c).state, "Heal")
            if w.tick % 200 == 0:
                w.health += 1  # regen
            w.tick += EVERY
        self.assertEqual(m.idle.events, [])

    def test_plan_wait_hold_is_dropped_for_the_next_goal(self):
        w = corridor()
        policy = Policy(kind="scripted", goals=["hold", "explore"])
        plan = Plan.from_policy(policy, dict(PARAM_DEFAULTS))
        m = Memory()
        c = ctx(policy, m, plan=plan)
        out = run_until(w, c, LIMIT - EVERY)
        self.assertIsNone(out.intents, "holding the plan wait")
        self.assertEqual(plan.current()["op"], "wait")
        out = dispatch(w, c)
        [event] = m.idle.events
        self.assertEqual(event["dropped_op"]["op"], "wait")
        self.assertEqual(plan.current()["op"], "explore_area", "the next plan goal")
        self.assertEqual(out.state, "Explore")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_goal_whose_steps_never_move_the_character_is_given_up(self):
        # Every decision sends a step east; the character never moves.
        w = corridor()
        m = Memory()
        c = ctx(Policy(kind="scripted", goals=["goto", "explore"], goto=(25, 0)), m)
        out = run_until(w, c, LIMIT - EVERY)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(nav_stuck.active(m, w).goal, "goto")
        self.assertEqual(m.nav_stuck.stuck_signals, [], "stuck detection alone has not given up yet")
        dispatch(w, c)
        [event] = m.idle.events
        self.assertEqual((event["goal"], event["target"]), ("goto", [25, 0]))
        [signal] = m.nav_stuck.stuck_signals
        self.assertEqual(signal["escalation"][-1], "idle")
        self.assertTrue(nav_stuck.backed_off(m, "goto", 1, (25, 0), w.tick))

    def test_downed_is_exempt(self):
        w = corridor()
        m = Memory()
        c = ctx(Policy(kind="scripted", goals=["goto", "explore"], goto=(25, 0)), m)
        w.alive = False
        out = run_until(w, c, 5 * LIMIT)
        self.assertEqual(out.state, "Downed")
        w.alive = True
        run_until(w, c, w.tick + LIMIT - 2 * EVERY)
        self.assertEqual(m.idle.events, [], "the clock starts again at respawn")
        self.assertEqual(m.nav_stuck.stuck_signals, [])

    def test_redirect_fires_again_only_after_another_idle_minute(self):
        w = pen()
        m = Memory()
        c = ctx(Policy(kind="scripted", goals=["explore"]), m)
        w.health = w.max_health
        run_until(w, c, 2 * LIMIT - EVERY)
        self.assertEqual(len(m.idle.events), 1)
        run_until(w, c, 2 * LIMIT)
        self.assertEqual(len(m.idle.events), 2)


class IdleRunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", Policy(goals=["explore"]), Path("t.toml"))
        self.r = Runner(cfg, mock.Mock(), 1, threading.Event(), out=lambda _: None)
        self.addCleanup(self.r.trace.close)
        self.r.world = corridor()

    def result(self, verb: str, outcome: str = "applied") -> None:
        r = self.r
        r.mem.pending = {"verb": verb}
        r.on_result({"tick": r.world.tick, "outcome": outcome, "index": 0}, 0)

    def test_world_changing_verbs_are_productive_and_waits_are_not(self):
        r = self.r
        idle_watchdog.observe(r.mem, r.world)
        r.world.tick = 500
        self.result("Wait")
        self.assertEqual(idle_watchdog.idle_ticks(r.mem, r.world.tick), 500)
        self.result("Take", outcome="applied_no_effect")
        self.assertEqual(idle_watchdog.idle_ticks(r.mem, r.world.tick), 500)
        self.result("Take")
        self.assertEqual(idle_watchdog.idle_ticks(r.mem, r.world.tick), 0)

    def test_redirect_is_traced(self):
        r = self.r
        r.mem.idle.events.append({"event": "idle_redirect", "state": "Heal", "cell": [0, 0], "ticks_idle": 600})
        r.acceptance = mock.Mock()
        r.trace_idle_redirects()
        r.acceptance.on_idle_redirect.assert_called_once()
        self.assertEqual(r.mem.idle.events, [])


class IdleGateTest(unittest.TestCase):
    def metrics(self) -> SurvivalAcceptanceMetrics:
        return SurvivalAcceptanceMetrics()

    def tick(self, g, w, m):
        g.note_survival_tick(
            w, m, state="Explore", reason="", intents=None, policy=Policy(), params=dict(PARAM_DEFAULTS), knowledge=None
        )

    def test_idle_stretch_past_the_limit_fails_the_gate(self):
        g, w, m = self.metrics(), corridor(), Memory()
        idle_watchdog.observe(m, w)
        w.tick = IDLE_GATE_SECONDS * 10
        self.tick(g, w, m)
        self.assertEqual(g.survival_failures(), [])
        w.tick += 10
        self.tick(g, w, m)
        g.on_idle_redirect({})
        self.assertEqual(g.idle_redirects, 1)
        self.assertEqual(len(g.survival_failures()), 1)
        self.assertIn("idle 91 s", g.survival_failures()[0])
        self.assertIn("idle redirects: 1 (longest idle: 91 s)", g.survival_summary_lines())


if __name__ == "__main__":
    unittest.main()
