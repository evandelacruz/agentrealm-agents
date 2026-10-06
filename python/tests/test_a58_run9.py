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
from agentrealm_agent.brain import UNPLACED_SELF_REFRESH, choose_call
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.flee import FLEE_PROBE_TICKS
from agentrealm_agent.world import Entity, WorldModel, chebyshev
from agentrealm_agent.zone_discovery import apply_zone

SURVIVAL = {"Escape", "Retreat", "Heal", "Fight", "Flee"}
STEP_TICKS = 4  # default movement_speed 2500: one step about every 4 ticks
SWING_TICKS = 15  # survival.HOSTILE_ATTACK_INTERVAL_TICKS


def world(at=(10, 10)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=8, health=10, max_health=10, lives=9)
    for x in range(-40, 61):
        for y in range(-40, 61):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


def ctx(on_hostile: str = "flee") -> PlayContext:
    policy = Policy(kind="scripted", goals=["explore"], on_hostile=on_hostile, hostile=["npc"], pickup=True)
    return PlayContext(Memory(), policy, random.Random(0), params=dict(PARAM_DEFAULTS))


def hit(w: WorldModel, npc_id: int = 7, amount: int = 2) -> None:
    """The pursuer's swing lands this tick."""
    w.apply_events([{"tick": w.tick, "events": [
        {"kind": "Attacked"},
        {"kind": "Damaged", "amount": amount, "source_kind": "npc", "source_id": npc_id},
    ]}])
    w.health -= amount


def chase(w: WorldModel, npc: Entity) -> None:
    """The NPC steps one cell toward us, staying off our cell."""
    x, y = npc.pos
    tx, ty = w.pos
    nx, ny = x + (tx > x) - (tx < x), y + (ty > y) - (ty < y)
    if (nx, ny) != w.pos:
        npc.pos = (nx, ny)


def pursue(w: WorldModel, c: PlayContext, *, npc_every: int, ticks: int = 80, start_gap: int = 1, swings: bool = True):
    """Live cadence under threat: one decision per tick (urgent polling), our
    step lands every ``STEP_TICKS``, the NPC steps toward us every
    ``npc_every`` ticks and swings every ``SWING_TICKS`` while adjacent.
    Returns (tick, outcome) per decision; the NPC opens with a hit."""
    npc = Entity("npc", 7, (w.pos[0] + start_gap, w.pos[1]), code="pursuer")
    w.entities = [npc]
    hit(w)
    last_move = last_swing = w.tick
    outs = []
    for _ in range(ticks):
        w.tick += 1
        out = dispatch(w, c)
        outs.append((w.tick, out))
        if out.intents and out.intents[0]["verb"] == "SetPosition" and w.tick - last_move >= STEP_TICKS:
            w.pos = w.terrain_center = (out.intents[0]["x"], out.intents[0]["y"])
            last_move = w.tick
        if w.tick % npc_every == 0:
            chase(w, npc)
        if swings and chebyshev(npc.pos, w.pos) <= 1 and w.tick - last_swing >= SWING_TICKS:
            hit(w)
            last_swing = w.tick
    return outs


class PursuerNotOutrunTest(unittest.TestCase):
    """Flee is not a death march: a pursuer that keeps pace makes it fight or retreat."""

    def test_no_safe_tile_known_fights_back(self):
        """Even a fight the estimate says we lose: running and retreating both failed."""
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 0.0
        outs = [o for _, o in pursue(w, c, npc_every=STEP_TICKS)]
        verbs = [o.intents[0]["verb"] for o in outs if o.intents]
        self.assertIn("Use", verbs, [o.reason for o in outs])
        self.assertTrue(all(o.state in SURVIVAL for o in outs), [o.state for o in outs])

    def test_safe_tile_known_retreats_to_it(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100  # healthy: Retreat's own health rule stays quiet
        c.params["risk"] = 0.0  # cautious: an unmeasured pursuer is one we would lose to
        apply_zone(w, 1, -20, -20, {"safe": True})
        reasons = [o.reason for _, o in pursue(w, c, npc_every=STEP_TICKS)]
        self.assertTrue(any("retreat → safe (-20, -20)" in r for r in reasons), reasons)

    def test_a_pursuer_we_beat_is_fought_even_with_a_safe_tile_known(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 1.0  # bold: the win estimate decides
        apply_zone(w, 1, -20, -20, {"safe": True})
        reasons = [o.reason for _, o in pursue(w, c, npc_every=STEP_TICKS)]
        given_up = [r for r in reasons if r.startswith("not outrunning")]
        self.assertTrue(given_up, reasons)
        self.assertEqual(given_up[0], "not outrunning npc 7: fight npc 7")
        self.assertFalse(any("retreat" in r for r in reasons), reasons)

    def test_a_hitter_out_of_weapon_reach_is_not_walked_back_to(self):
        """Running failed, we would lose, no safe tile: swing back only at the hitter in reach, never close in (review on #107)."""
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 0.0
        w.entities = [Entity("npc", 7, (12, 10), code="pursuer")]  # in range, two cells: out of weapon reach
        m = c.memory
        m.state, m.flee_since, m.flee_failed, m.flee_gaps = "Flee", w.tick, True, [(w.tick, 2)]
        w.tick += 5
        hit(w, npc_id=7)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)
        self.assertNotIn("close on", out.reason)
        w.entities[0].pos = (11, 10)  # now in reach
        w.tick += 1
        hit(w, npc_id=7)
        self.assertEqual(dispatch(w, c).reason, "not outrunning npc 7: fight npc 7")

    def test_a_hostile_in_reach_that_is_not_the_hitter_is_not_fought(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 0.0
        w.entities = [Entity("npc", 9, (11, 10), code="bystander")]  # adjacent; npc 7 hits from out of view
        m = c.memory
        m.state, m.flee_since, m.flee_failed, m.flee_gaps = "Flee", w.tick, True, [(w.tick, 1)]
        w.tick += 5
        hit(w, npc_id=7)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)

    def test_a_failed_retreat_leaves_the_oscillation_escape_to_flee(self):
        """Flee gave up running, the known safe tile is walled off: Retreat's try sends
        nothing, and the paced cells must still reach Flee's own escape (A15, review on #107)."""
        w, c = world(), ctx()
        for y in range(-2, 3):
            for x in (-2, 2):
                w.view.tiles[(x, y)] = "wall"
        for x in range(-2, 3):
            for y in (-2, 2):
                w.view.tiles[(x, y)] = "wall"
        apply_zone(w, 1, 0, 0, {"safe": True})  # walled in: unreachable
        w.entities = [Entity("npc", 7, (11, 10), code="pursuer")]
        m = c.memory
        m.state, m.flee_since, m.flee_failed, m.flee_gaps = "Flee", w.tick, True, [(w.tick, 1)]
        here, back = (10, 10), (9, 9)  # Flee has been pacing between these (A15 guard)
        m.flee_path = [back]
        m.nav_stuck.cells_map = w.map_id
        m.nav_stuck.recent_cells = [back, here, back, here, back]
        m.nav_stuck.recent_moves = [("", "Flee")] * 5
        m.nav_stuck.last_move = ("", "Flee")
        w.tick += 5
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertTrue(out.reason.startswith("flee npc 7"), out.reason)
        self.assertEqual(m.flee_avoid, {back})
        self.assertNotIn(back, m.flee_path)

    def test_flees_retreat_keeps_off_the_paced_cell(self):
        """Running failed and the safe tile is straight west: Retreat's step keeps off the paced cell (A15)."""
        w, c = world(), ctx()
        apply_zone(w, 1, 4, 10, {"safe": True})
        w.entities = [Entity("npc", 7, (11, 10), code="pursuer")]
        m = c.memory
        m.state, m.flee_since, m.flee_failed, m.flee_gaps = "Flee", w.tick, True, [(w.tick, 1)]
        here, back = (10, 10), (9, 9)  # (9, 9) is Retreat's first step with nothing paced
        m.nav_stuck.cells_map = w.map_id
        m.nav_stuck.recent_cells = [back, here, back, here, back]
        m.nav_stuck.recent_moves = [("", "Flee")] * 5
        m.nav_stuck.last_move = ("", "Flee")
        w.tick += 5
        out = dispatch(w, c)
        self.assertIn("retreat → safe (4, 10)", out.reason)
        self.assertNotEqual((out.intents[0]["x"], out.intents[0]["y"]), back)

    def test_a_pursuer_keeping_pace_without_hitting_is_fought_once_the_gap_stalls(self):
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 1.0
        start = w.tick
        outs = pursue(w, c, npc_every=STEP_TICKS, start_gap=2, swings=False)
        early = [o.reason for t, o in outs if t - start <= FLEE_PROBE_TICKS]
        self.assertEqual(set(early), {"flee npc 7"}, early)
        self.assertTrue(any(o.reason.startswith("not outrunning npc 7:") for _, o in outs), [o.reason for _, o in outs])

    def test_a_slower_pursuer_at_urgent_cadence_is_outrun_not_fought(self):
        """Decisions come every tick but steps every 4: a few decisions with the gap
        unchanged are not a failed escape (review on #107)."""
        w, c = world(), ctx()
        w.health = w.max_health = 100
        c.params["risk"] = 1.0  # a fight we would win: a latched give-up would show as one
        outs = pursue(w, c, npc_every=3 * STEP_TICKS, ticks=40)
        flee = [o.reason for _, o in outs if o.state == "Flee"]
        self.assertTrue(flee)
        self.assertFalse([r for r in flee if r.startswith("not outrunning")], flee)

    def test_trap_and_hazard_damage_is_not_a_pursuer(self):
        w, c = world(), ctx()
        w.entities = [Entity("npc", 7, (16, 10))]  # in view, out of range
        w.apply_events([{"tick": w.tick, "events": [
            {"kind": "Damaged", "amount": 2, "source_kind": "trap", "source_id": 3},
            {"kind": "Damaged", "amount": 1, "source_kind": "occupy"},
        ]}])
        self.assertIsNone(w.attacked_tick)
        self.assertNotEqual(dispatch(w, c).state, "Flee")
        w.apply_events([{"tick": w.tick, "events": [{"kind": "Attacked"}]}])
        self.assertEqual(w.attacked_tick, w.tick, "an Attacked alone is a hostile's swing")


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
        for i in range(16):
            w.tick += 1
            npc.pos = (w.pos[0] + (2 if i % 2 else 3), w.pos[1])
            if i % 8 == 7:
                hit(w)  # it keeps landing hits between its steps out of range
            out = dispatch(w, c)
            self.assertEqual(out.state, "Flee", out.reason)

    def test_flee_ends_once_the_pursuer_is_shaken(self):
        w, c = world(), ctx()
        w.entities = [Entity("npc", 7, (14, 10))]
        w.tick = 100
        hit(w)
        w.tick += 100  # long past the last hit, out of range
        self.assertNotEqual(dispatch(w, c).state, "Flee")

    def test_a_bystander_in_view_is_not_the_pursuer(self):
        """Hit by npc 7, out of view; npc 9 stands seven cells away: Flee does not run from npc 9 (review on #107)."""
        w, c = world(), ctx()
        w.entities = [Entity("npc", 9, (17, 10))]
        hit(w, npc_id=7)
        self.assertNotEqual(dispatch(w, c).state, "Flee")
        w.tick += SWING_TICKS
        hit(w, npc_id=7)  # a second hit from the unseen npc 7
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Flee", out.reason)
        self.assertFalse(any(r in out.reason for r in ("flee npc 9", "close on npc 9", "fight npc 9")), out.reason)

    def test_an_unseen_hit_does_not_blame_an_old_attacker(self):
        """npc 7 hits, then idles 6 cells away; 200 ticks later an unseen Attacked lands (review on #107)."""
        w, c = world(), ctx()
        w.entities = [Entity("npc", 7, (11, 10), code="pursuer")]
        hit(w, npc_id=7)
        w.entities[0].pos = (16, 10)
        w.tick += 200
        w.apply_events([{"tick": w.tick, "events": [{"kind": "Attacked"}]}])
        out = dispatch(w, c)
        self.assertNotIn("flee npc 7", out.reason)
        self.assertNotIn("fight npc 7", out.reason)
        self.assertIsNone(w.attacker)

    def test_a_sourceless_attacked_in_the_same_tick_keeps_the_named_hitter(self):
        w = world()
        w.apply_events([{"tick": w.tick, "events": [
            {"kind": "Damaged", "amount": 2, "source_kind": "npc", "source_id": 7},
            {"kind": "Attacked"},
        ]}])
        self.assertEqual(w.attacker, ("npc", 7))

    def test_fight_policy_does_not_flee_a_beatable_attacker_past_range(self):
        """``on_hostile = "fight"`` keeps its rule: a beatable NPC 3 away that just hit us is no reason to run."""
        w, c = world(), ctx("fight")
        w.health = w.max_health = 100
        c.params["risk"] = 1.0
        w.entities = [Entity("npc", 7, (13, 10), code="pursuer")]
        hit(w)
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

    def test_alive_but_not_placed_ticks_and_rereads_self_with_a_backoff(self):
        """Not placed yet: poll ticks for the events, re-read self only every few windows (review on #107)."""
        w, m, pol = world(), Memory(need_position=True, need_self=False), ctx().policy
        w.pos = None
        w.apply_self({"lives": 9, "alive": True, "placed": False})
        calls = []
        for _ in range(2 * UNPLACED_SELF_REFRESH):
            call = choose_call(w, m, pol)
            calls.append(call)
            m.windows_since_self = 0 if call == "self" else m.windows_since_self + 1
        self.assertNotIn("position", calls)
        self.assertEqual(calls.count("self"), 1, calls)
        self.assertEqual(calls.index("self"), UNPLACED_SELF_REFRESH)

    def test_a_wake_reads_position_at_once(self):
        """Self said asleep and not placed; a round trip says awake, and nothing else (review on #107)."""
        w, m, pol = world(), Memory(need_position=True, need_self=False), ctx().policy
        w.pos = None
        w.apply_self({"lives": 9, "alive": True, "asleep": True, "placed": False})
        self.assertEqual(choose_call(w, m, pol), "tick")
        w.apply_observation({"version": 2, "delta": {"asleep": False}})
        self.assertEqual(choose_call(w, m, pol), "position")
        # Sync re-reads self after the wake; GetSelf may still say not placed.
        w.apply_self({"lives": 9, "alive": True, "asleep": False, "placed": False})
        self.assertEqual(choose_call(w, m, pol), "position")
        w.apply_position({"map_id": 1, "x": 3, "y": 4})
        w.apply_self({"lives": 9, "alive": True, "asleep": False, "placed": False})
        self.assertFalse(w.placed, "after the position read, self is believed again")

    def test_a_wake_seen_only_in_a_self_read_reads_position(self):
        w, m, pol = world(), Memory(need_position=True, need_self=False), ctx().policy
        w.pos = None
        w.apply_self({"lives": 9, "alive": True, "asleep": True, "placed": False})
        w.apply_self({"lives": 9, "alive": True, "asleep": False, "placed": False})
        self.assertEqual(choose_call(w, m, pol), "position")

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
