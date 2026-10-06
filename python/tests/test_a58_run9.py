"""A58 run 9 offline: died fleeing one pursuer, then position reads while downed (A9, A5).

Live run 9 fled a single NPC that kept pace and kept hitting; Explore and
Investigate queues ran between the flee steps whenever it stepped just out
of ``hostile_range``, and the character died fleeing. Once dead, the runner
asked for position four times and drew ``409 not_on_map`` each time.

Rebuilt here through the real dispatcher and scheduler on open ground.
"""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import choose_call
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.flee import FLEE_PROBE_STEPS
from agentrealm_agent.world import Entity, WorldModel, chebyshev
from agentrealm_agent.zone_discovery import apply_zone

SURVIVAL = {"Escape", "Retreat", "Heal", "Fight", "Flee"}


def world(at=(10, 10)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=8, health=10, max_health=10, lives=9)
    for x in range(-20, 41):
        for y in range(-20, 41):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


def ctx() -> PlayContext:
    policy = Policy(kind="scripted", goals=["explore"], on_hostile="flee", hostile=["npc"], pickup=True)
    return PlayContext(Memory(), policy, random.Random(0), params=dict(PARAM_DEFAULTS))


def hit(w: WorldModel, amount: int = 2) -> None:
    """The pursuer's swing lands this tick."""
    w.apply_events([{"tick": w.tick, "events": [{"kind": "Attacked"}, {"kind": "Damaged", "amount": amount}]}])
    w.health -= amount


def chase(w: WorldModel, npc: Entity) -> None:
    """The NPC steps one cell toward us, staying off our cell."""
    x, y = npc.pos
    tx, ty = w.pos
    nx, ny = x + (tx > x) - (tx < x), y + (ty > y) - (ty < y)
    if (nx, ny) != w.pos:
        npc.pos = (nx, ny)


def land(w: WorldModel, out) -> None:
    w.tick += 10
    if out.intents and out.intents[0]["verb"] == "SetPosition":
        w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        w.terrain_center = w.pos


class PursuerNotOutrunTest(unittest.TestCase):
    """Flee is not a death march: a pursuer that keeps pace makes it fight or retreat."""

    def _pursue(self, w: WorldModel, c: PlayContext, decisions: int = 8) -> list:
        npc = Entity("npc", 7, (w.pos[0] + 1, w.pos[1]), code="pursuer")
        w.entities = [npc]
        hit(w)
        outs = []
        for _ in range(decisions):
            out = dispatch(w, c)
            outs.append(out)
            land(w, out)
            chase(w, npc)
            if chebyshev(npc.pos, w.pos) <= 1:
                hit(w)
        return outs

    def test_no_safe_tile_known_fights_back(self):
        """Even a fight the estimate says we lose: running and retreating both failed."""
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 0.0
        outs = self._pursue(w, c)
        verbs = [o.intents[0]["verb"] for o in outs if o.intents]
        self.assertIn("Use", verbs, [o.reason for o in outs])
        self.assertTrue(all(o.state in SURVIVAL for o in outs), [o.state for o in outs])

    def test_safe_tile_known_retreats_to_it(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100  # healthy: Retreat's own health rule stays quiet
        c.params["risk"] = 0.0  # cautious: an unmeasured pursuer is one we would lose to
        apply_zone(w, 1, 4, 4, {"safe": True})
        outs = self._pursue(w, c)
        reasons = [o.reason for o in outs]
        self.assertTrue(any("retreat → safe (4, 4)" in r for r in reasons), reasons)

    def test_a_pursuer_we_beat_is_fought_even_with_a_safe_tile_known(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 1.0  # bold: the win estimate decides
        apply_zone(w, 1, 4, 4, {"safe": True})
        reasons = [o.reason for o in self._pursue(w, c)]
        self.assertEqual(reasons[1], "not outrunning npc 7: fight npc 7", "the first hit after Flee began ends the run")
        self.assertFalse(any("retreat" in r for r in reasons), reasons)

    def test_a_pursuer_keeping_pace_without_hitting_is_fought_once_the_gap_stalls(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 1.0
        npc = Entity("npc", 7, (11, 10), code="pacer")
        w.entities = [npc]
        hit(w)
        reasons = []
        for _ in range(6):
            out = dispatch(w, c)
            reasons.append(out.reason)
            land(w, out)
            chase(w, npc)
        self.assertEqual(reasons[:FLEE_PROBE_STEPS], ["flee npc 7"] * FLEE_PROBE_STEPS, reasons)
        self.assertTrue(reasons[FLEE_PROBE_STEPS].startswith("not outrunning npc 7:"), reasons)

    def test_a_pursuer_left_behind_is_not_fought(self):
        """Fleeing that works stays fleeing: no hit after Flee began, gap growing."""
        w, c = world(), ctx()
        w.entities = [Entity("npc", 7, (11, 10), code="slow")]
        hit(w)
        for _ in range(6):
            out = dispatch(w, c)
            if out.state != "Flee":
                break
            self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)
            land(w, out)


class FleeKeepsThePursuerTest(unittest.TestCase):
    """A pursuer that hit us is fled even just past ``hostile_range``, so no other
    state's queue runs between flee steps; Flee's reflex replaces any held queue."""

    def test_pursuer_just_out_of_range_after_a_hit_is_still_fled(self):
        w, c = world(), ctx()
        w.entities = [Entity("npc", 7, (13, 10))]  # 3 away: past hostile_range 2
        hit(w)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee", out.reason)
        self.assertTrue(out.reflex, "replaces any held Explore or Investigate queue")

    def test_alternating_range_never_lets_explore_or_investigate_in(self):
        w, c = world(), ctx()
        npc = Entity("npc", 7, (11, 10))
        w.entities = [npc]
        hit(w)
        for i in range(8):
            npc.pos = (w.pos[0] + (2 if i % 2 else 3), w.pos[1])
            if i % 2:
                hit(w)  # it keeps landing hits between its steps out of range
            out = dispatch(w, c)
            self.assertEqual(out.state, "Flee", out.reason)
            land(w, out)

    def test_flee_ends_once_the_pursuer_is_shaken(self):
        w, c = world(), ctx()
        w.entities = [Entity("npc", 7, (14, 10))]
        w.tick = 100
        hit(w)
        w.tick += 100  # long past the last hit, out of range
        self.assertNotEqual(dispatch(w, c).state, "Flee")


class NoPositionReadWhileDownedTest(unittest.TestCase):
    """After Died, position answers 409 not_on_map: read self, then wait for Respawned."""

    def test_died_reads_self_not_position(self):
        w, m, pol = world(), Memory(need_position=False, need_self=False), ctx().policy
        w.apply_events([{"tick": 5, "events": [{"kind": "Died", "cause": "killed"}]}])
        m.need_position = True
        self.assertNotEqual(choose_call(w, m, pol), "position")

    def test_downed_self_read_waits_on_ticks(self):
        w, m, pol = world(), Memory(need_position=True, need_self=False), ctx().policy
        w.apply_events([{"tick": 5, "events": [{"kind": "Died", "cause": "killed"}]}])
        w.apply_self({"lives": 8, "alive": False, "placed": False})
        for _ in range(4):
            self.assertEqual(choose_call(w, m, pol), "tick")

    def test_respawned_reads_position_again(self):
        w, m, pol = world(), Memory(need_position=True, need_self=False), ctx().policy
        w.apply_events([{"tick": 5, "events": [{"kind": "Died", "cause": "killed"}]}])
        w.apply_self({"lives": 8, "alive": False, "placed": False})
        w.apply_events([{"tick": 9, "events": [{"kind": "Respawned", "map_id": 1, "x": 0, "y": 0}]}])
        self.assertEqual(choose_call(w, m, pol), "position")

    def test_runner_reads_self_after_respawned_then_position(self):
        """The respawn window from the runner's side: no position read until Respawned."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(config, "STATE_DIR", Path(tmp.name)):
            r = Runner(CharacterConfig("T", "sandbox", ctx().policy, Path("t.toml")), mock.Mock(), 1,
                       threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        r.world, pol = world(), r.cfg.policy
        r.mem.need_self = r.mem.need_position = False

        def events(*evs):
            r.on_events(r.world.apply_events([{"tick": 5, "events": list(evs)}]))

        events({"kind": "Died", "cause": "killed"})
        calls = [choose_call(r.world, r.mem, pol)]
        self.assertEqual(calls, ["self"])
        r.world.apply_self({"lives": 8, "alive": False, "placed": False})
        r.mem.need_self = False
        calls += [choose_call(r.world, r.mem, pol) for _ in range(3)]
        events({"kind": "Respawned", "map_id": 1, "x": 0, "y": 0})
        calls.append(choose_call(r.world, r.mem, pol))
        r.world.apply_self({"lives": 8, "alive": True, "placed": True})
        r.mem.need_self = False
        calls.append(choose_call(r.world, r.mem, pol))
        self.assertEqual(calls, ["self", "tick", "tick", "tick", "self", "position"])


if __name__ == "__main__":
    unittest.main()
