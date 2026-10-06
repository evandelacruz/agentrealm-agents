"""A36: M4 done-when as an offline fixture (no server, no model).

An invented test world: a sign whose text is the clue, a torch for sale, an
entrance, and a hedge that burns. The real ``Runner`` and states play it
against ``TestWorldServer``; ``ClueModel`` stands in for the LLM and answers
the clue with the plan from docs/PLAYABLE_AGENT_PLAN.md (Strategist example).
``M4AcceptanceMetrics`` checks the strategist planned each op and the states
carried it out.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.executor import step_landing
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.m4_acceptance import M4AcceptanceMetrics, op_key
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.strategist import Strategist, StrategistConfig
from agentrealm_agent.world import WorldModel

MAP = 7
X0, Y0, X1, Y1 = 100, 34, 124, 46  # walls on the border, grass inside
START = (104, 40)
SIGN = (102, 38)
CLUE = "The cave door is behind the hedge wall to the east. Hedges burn; the shop sells torches."
TORCH_ID, TORCH_PRICE = 900, 5
TORCH_AT = (108, 40)
HELD_TORCH_ID = 901
HEDGES = {(119, y) for y in range(Y0 + 1, Y1)}  # a wall, so no single cell is odd (A31)
HEDGE = (119, 40)
ENTRANCE = (122, 40)  # behind the hedges

# What the strategist should answer that clue with (docs/PLAYABLE_AGENT_PLAN.md, Strategist).
BUY = {"op": "buy", "code": "torch"}
TO_HEDGE = {"op": "travel", "to": "point", "x": HEDGE[0] - 1, "y": HEDGE[1]}
BREAK = {"op": "break_block", "x": HEDGE[0], "y": HEDGE[1], "capability": "burn"}
ENTER = {"op": "travel", "to": "entrance", "x": ENTRANCE[0], "y": ENTRANCE[1]}
REQUIRED = (BUY, TO_HEDGE, BREAK, ENTER)
PLAN = {"goals": [dict(BUY, why="clue: hedges burn"), TO_HEDGE, BREAK, ENTER], "notes": "burn through the hedge"}
# The opening stack: read the sign. Nothing reads on its own any more (the
# planner decides what to investigate), so the clue comes from this op.
READ_SIGN = {"op": "read", "x": SIGN[0], "y": SIGN[1], "why": "a sign in sight"}


def block_at(p: tuple[int, int], burnt: set) -> str:
    x, y = p
    if not (X0 <= x <= X1 and Y0 <= y <= Y1):
        return ""
    if x in (X0, X1) or y in (Y0, Y1):
        return "stone_wall"
    if p == SIGN:
        return "stone_sign"
    if p == ENTRANCE:
        return "framed_door"
    if p in HEDGES and p not in burnt:
        return "hedge"
    return "grass"


class TestWorldServer:
    """Plays the invented world one intent per tick, like the real queue.

    Each window advances one tick. ``Step`` moves onto open ground, ``Read``
    of the sign returns the clue, ``Take`` of the priced torch spends gems,
    ``Arm`` arms a held supply (a torch is not ``Wear``-able), and ``Use`` of a hedge with the torch armed
    burns it (``BlockChanged``). Every tick answers with a complete snapshot.
    An applied ``Wait`` has no result, and a queue that ends carries
    ``finished_queue`` until the next submit (B133).
    """

    def __init__(self, stop: threading.Event, windows: int = 3000):
        self.stop = stop
        self.windows = windows
        self.tick_now = 100
        self.pos = START
        self.gems = 20
        self.torch_for_sale = True
        self.held: list[dict] = []
        self.armed: dict | None = None
        self.burnt: set = set()
        self.queue: list[dict] = []
        self.queue_id: str | None = None
        self.finished: dict | None = None  # finished_queue while the queue is empty
        self.start = self.done = self.submits = 0
        self.results: list[dict] = []
        self.events: list[dict] = []
        self.version = 0
        self.on_wait = lambda: None

    # --- the clock ---

    def wait(self, not_before: float = 0.0) -> None:
        self.windows -= 1
        if self.windows < 0:
            self.stop.set()
        self.on_wait()
        self.tick_now += 1
        while self.done < len(self.queue) and self.start + self.done <= self.tick_now:
            t, i = self.start + self.done, self.done
            self.done += 1
            res = {"tick": t, "queue_id": self.queue_id, "index": i, "outcome": "applied"}
            res.update(self._run(self.queue[i], t))
            if self.queue[i]["verb"] != "Wait" or res["outcome"] == "rejected":
                self.results.append(res)  # an applied Wait has no result (B133)
            if res["outcome"] == "rejected" or self.done == len(self.queue):
                self.finished = {"queue_id": self.queue_id, "length": len(self.queue)}
            if res["outcome"] == "rejected":
                self.queue, self.done = [], 0

    def _run(self, intent: dict, t: int) -> dict:
        verb = intent["verb"]
        if verb == "Step":
            to = step_landing(self.pos, intent["direction"])
            if block_at(to, self.burnt) not in ("grass", "framed_door"):
                return {"outcome": "rejected", "rejection": {"code": "blocked", "retryability": "permanent"}}
            self.pos = to
        elif verb == "Read":
            target = intent["target"]
            if target.get("kind") == "block" and (target["x"], target["y"]) == SIGN:
                return {"text": CLUE}
        elif verb == "Take":
            if not (self.torch_for_sale and intent["supply_id"] == TORCH_ID and self._near(TORCH_AT)):
                return {"outcome": "rejected", "rejection": {"code": "not_adjacent", "retryability": "transient"}}
            self.torch_for_sale = False
            self.gems -= TORCH_PRICE
            self.held.append({"id": HELD_TORCH_ID, "supply_subtype_code": "torch"})
            self.events.append({"tick": t, "kind": "SupplyTaken", "supply_id": TORCH_ID})
        elif verb == "Wear":
            return {"outcome": "rejected", "rejection": {"code": "not_wearable", "retryability": "permanent"}}
        elif verb == "Arm":
            self.armed = next((s for s in self.held if s["id"] == intent["supply_id"]), None)
        elif verb == "Use":
            target = intent.get("target") or {}
            p = (target.get("x"), target.get("y"))
            if target.get("kind") != "block" or not self._near(p):
                return {"outcome": "rejected", "rejection": {"code": "target_out_of_range", "retryability": "transient"}}
            if p not in HEDGES or p in self.burnt or (self.armed or {}).get("supply_subtype_code") != "torch":
                return {"outcome": "applied_no_effect"}
            self.burnt.add(p)
            self.events.append({"tick": t, "kind": "BlockChanged", "map_id": MAP, "x": p[0], "y": p[1], "block_type": "grass"})
        return {}

    def _near(self, p) -> bool:
        return max(abs(p[0] - self.pos[0]), abs(p[1] - self.pos[1])) <= 1

    # --- the API ---

    def world(self, cid):
        return {"tick_rate_hz": 10, "code": "testworld", "status": "live"}

    def minimap(self, cid):
        # The entrance is a minimap mark, so Travel resolves `travel:entrance:122,40` (A27).
        return {"maps": [{"map_id": MAP, "entrances": [{"x": ENTRANCE[0], "y": ENTRANCE[1]}]}]}

    def terrain(self, cid, map_id, x0, y0, width, height):
        legend = {
            "g": {"block_type": "grass"},
            "w": {"block_type": "stone_wall"},
            "s": {"block_type": "stone_sign", "readable": True},
            "d": {"block_type": "framed_door"},
            "h": {"block_type": "hedge"},
        }
        sym = {v["block_type"]: k for k, v in legend.items()}
        rows = [
            "".join(sym.get(block_at((x, y), self.burnt), "?") for x in range(x0, x0 + width))
            for y in range(y0, y0 + height)
        ]
        return {"tick": self.tick_now, "map_id": map_id, "x0": x0, "y0": y0, "width": width, "height": height,
                "legend": legend, "rows": rows}

    def _supplies(self) -> list[dict]:
        if not self.torch_for_sale:
            return []
        return [{"id": TORCH_ID, "x": TORCH_AT[0], "y": TORCH_AT[1], "supply_subtype_code": "torch",
                 "gem_price": TORCH_PRICE}]

    def entities(self, cid, map_id, x0, y0, width, height):
        return {"tick": self.tick_now, "supplies": self._supplies()}

    def self_(self, cid):
        return {"perception_range": 25, "movement_range": 1, "alive": True, "lives": 3}

    def position(self, cid):
        return {"map_id": MAP, "x": self.pos[0], "y": self.pos[1]}

    def zone(self, cid, map_id, x, y):
        return {"tick": self.tick_now, "safe": False}

    def tick(self, cid, intents, *, snapshot_version=None):
        results, self.results = self.results, []
        events, self.events = self.events, []
        self.version += 1
        r = {
            "tick": self.tick_now,
            "window_remaining_ms": 0,
            "intent_results": results,
            "events_by_tick": [{"tick": e["tick"], "events": [e]} for e in events],
            "observation": {"version": self.version, "complete": True, "snapshot": {
                "position": {"map_id": MAP, "x": self.pos[0], "y": self.pos[1]},
                "alive": True, "lives": 3, "health": 10, "max_health": 10,
                "inventory": {"gems": self.gems, "armed": self.armed, "held": list(self.held), "chest": []},
                "entities": {"supplies": self._supplies()},
            }},
        }
        if intents is not None:
            self.submits += 1
            self.queue_id = f"q{self.submits}"
            self.queue, self.start, self.done = list(intents), self.tick_now + 1, 0
            self.finished = None
            r["queue_id"] = self.queue_id
        if self.done < len(self.queue):
            r["queue"] = {"queue_id": self.queue_id, "next_index": self.done}
        elif self.finished is not None:
            r["finished_queue"] = self.finished
        return r


class ClueModel:
    """The fake LLM: answers the first prompt that carries the clue with
    ``reply``, and every later one without a ``goals`` key, so the stack is kept."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, messages):
        prompt = "\n".join(m["content"] for m in messages)
        self.prompts.append(prompt)
        answered = any(CLUE in p for p in self.prompts[:-1])
        if CLUE in prompt and not answered:
            return json.dumps(self.reply), {"prompt_tokens": 100, "completion_tokens": 50}
        return json.dumps({"notes": "keep"}), {"prompt_tokens": 100, "completion_tokens": 10}


class InlineStrategist(Strategist):
    """Answers on the test's clock (``serve_one`` each window), not a thread."""

    def start(self) -> None:
        pass


class TestWorldCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def play(self, reply: dict = PLAN, windows: int = 3000) -> tuple[M4AcceptanceMetrics, TestWorldServer, ClueModel]:
        stop = threading.Event()
        metrics = M4AcceptanceMetrics(required=REQUIRED)
        server = TestWorldServer(stop, windows)
        model = ClueModel(reply)
        strategist = InlineStrategist(config=StrategistConfig(provider="openai", model="m", api_key="k"), client=model)
        cfg = CharacterConfig("T", "testworld", Policy(goals=[], pickup=False, entity_refresh=20), Path("t.toml"))
        r = runner.Runner(cfg, metrics.wrap(server), 1, stop, out=lambda _: None,
                          knowledge=KnowledgeBase.empty("testworld"), acceptance=metrics, strategist=strategist)
        self.addCleanup(r.trace.close)
        r.world = WorldModel(character_id=1)
        r.mem = Memory()
        r.plan = Plan([dict(READ_SIGN)], dict(PARAM_DEFAULTS))

        def on_wait():
            strategist.serve_one(timeout=0)
            if not metrics.failures():
                stop.set()

        server.on_wait = on_wait
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: server.wait(nb)):
            r.run()
        return metrics, server, model


class M4DoneWhenTest(TestWorldCase):
    def test_clue_to_plan_to_buy_travel_break_enter(self):
        metrics, server, model = self.play()
        self.assertEqual(metrics.failures(), [], metrics.summary_lines())
        clue_calls = [i for i, p in enumerate(model.prompts) if CLUE in p]
        self.assertTrue(clue_calls, "a call carried the sign's clue")  # timer calls may come first
        self.assertEqual(server.gems, 20 - TORCH_PRICE, "the torch was bought")
        self.assertEqual(server.burnt, {HEDGE}, "the planned hedge burned, and no other")
        self.assertEqual(server.pos, ENTRANCE, "walked through the gap onto the entrance")
        self.assertGreater(server.windows, 0, "finished before the windows ran out")

    def test_plan_without_the_ops_fails(self):
        metrics, server, _ = self.play({"goals": [{"op": "wait", "seconds": 1, "why": "no idea"}], "notes": "no idea"}, windows=600)
        failures = metrics.failures()
        for op in REQUIRED:
            self.assertIn(f"strategist never planned {op_key(op)}", failures)
        self.assertFalse(server.burnt)


class RunnerActedOpTest(TestWorldCase):
    def test_before_tick_gets_the_op_this_round_acted_on(self):
        seen: list = []

        class Hooks(M4AcceptanceMetrics):
            def before_tick(self, w, m, *, acted_op=None, **kw):
                seen.append(acted_op)

        stop = threading.Event()
        server = TestWorldServer(stop, 10)
        cfg = CharacterConfig("T", "testworld", Policy(goals=[], pickup=False), Path("t.toml"))
        r = runner.Runner(cfg, server, 1, stop, out=lambda _: None, acceptance=Hooks())
        self.addCleanup(r.trace.close)
        r.world = WorldModel(character_id=1, map_id=MAP, pos=START, perception=25, tick=100)
        r.mem = Memory(need_self=False, need_position=False)
        r.plan.acted = dict(TO_HEDGE)  # left over from an earlier round
        r.tick()  # no plan op: the safe default acts on none
        self.assertEqual(seen, [None])


class MetricsTest(unittest.TestCase):
    """Each gate condition on its own, so breaking one fails a test."""

    def done(self) -> M4AcceptanceMetrics:
        m = M4AcceptanceMetrics(required=REQUIRED)
        m.on_strategist_trigger({"trigger": "clue", "text": CLUE})
        m.on_strategist_applied(list(PLAN["goals"]))
        for op, state in ((BUY, "Shop"), (TO_HEDGE, "Travel"), (BREAK, "Break"), (ENTER, "Travel")):
            self.tick(m, state, op)
            m.on_strategist_trigger({"trigger": "goal_done", "op": dict(op), "reason": "goal_done"})
        return m

    def tick(self, m, state, op, intents=({"verb": "Wait"},)):
        m.before_tick(WorldModel(character_id=1), Memory(), state=state, reason="test",
                      intents=list(intents) if intents is not None else None, policy=Policy(),
                      params=dict(PARAM_DEFAULTS), knowledge=None, acted_op=op)

    def test_all_conditions_met_passes(self):
        self.assertEqual(self.done().failures(), [])

    def test_needs_a_clue_trigger(self):
        m = self.done()
        m.clue_triggers = 0
        self.assertIn("no clue trigger reached the strategist", m.failures())

    def test_needs_each_op_planned(self):
        m = self.done()
        m.planned.discard(op_key(BREAK))
        self.assertEqual(m.failures(), [f"strategist never planned {op_key(BREAK)}"])

    def test_needs_each_op_run_by_its_state(self):
        m = M4AcceptanceMetrics(required=(BUY,))
        self.tick(m, "Explore", BUY)  # not Shop
        self.tick(m, "Shop", BUY, intents=None)  # a held queue sends nothing new
        self.tick(m, "Shop", None)  # Shop sent a queue, but not for the plan's buy (a restock, a reflex)
        self.assertNotIn(op_key(BUY), m.run)
        self.assertIn(f"Shop never ran {op_key(BUY)}", m.failures())
        self.tick(m, "Shop", BUY)
        self.assertIn(op_key(BUY), m.run)

    def test_another_state_taking_the_round_is_not_a_failure(self):
        m = self.done()
        self.tick(m, "Flee", BREAK)
        self.assertEqual(m.failures(), [])

    def test_needs_each_op_finished(self):
        m = self.done()
        m.finished.discard(op_key(ENTER))
        self.assertEqual(m.failures(), [f"{op_key(ENTER)} never finished"])

    def test_api_errors_fail(self):
        class Refusing:
            def tick(self, *a, **k):
                raise ApiError(429, "rate_limited")

        m = self.done()
        with self.assertRaises(ApiError):
            m.wrap(Refusing()).tick(1, None)
        self.assertTrue(any("API error" in f for f in m.failures()))


if __name__ == "__main__":
    unittest.main()
