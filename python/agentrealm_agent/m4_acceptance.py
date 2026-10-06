"""M4 acceptance metrics (A36): the strategist plans clue-driven ops and states run them.

The M4 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones): given clues from a
test world, the strategist plans the right ``buy``, ``travel`` and
``break_block`` operations and the state machine carries them out. The test
world is an offline fixture (``tests/test_m4_acceptance.py``): an invented map,
a clue in the knowledge base, a fake model and a fake server, played by the
real ``Runner`` and states. These metrics are what it checks, op by op:

- **planned**: an applied strategist answer had the op (extra fields allowed);
- **run**: the state that owns the op (``OP_DRIVERS``) sent a new queue that
  acted on it (``Plan.acted``: a step toward it, its Take or its Use). A round
  spent on a reflex, another goal or a higher-priority state (Flee, Heal) does
  not count, and is not a failure either;
- **finished**: the stack popped the op as done (a ``goal_done`` trigger).

At least one clue trigger must reach the strategist, and no request may fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .acceptance import AcceptanceHooks, CountingClient
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import WorldModel

# The states that carry out each gated op (``plan.OP_STATE``, A61).
OP_DRIVERS: dict[str, frozenset[str]] = {
    "buy": frozenset({"Shop"}),
    "travel": frozenset({"Travel"}),
    "break_block": frozenset({"Break"}),
}


def op_key(op: dict) -> str:
    """Stable key naming a gated op in summaries and failures."""
    name = op["op"]
    if name == "buy":
        return f"buy:{op['code']}"
    if name == "travel":
        return f"travel:{op['to']}:{op['x']},{op['y']}"
    if name == "break_block":
        return f"break_block:{op['x']},{op['y']}:{op['capability']}"
    return name


def op_matches(required: dict, candidate: dict) -> bool:
    """True when ``candidate`` has every field ``required`` names."""
    return all(candidate.get(k) == v for k, v in required.items())


@dataclass
class M4AcceptanceMetrics(AcceptanceHooks):
    """Counts clue triggers, and for each ``required`` op whether it was
    planned, run by its state, and finished."""

    required: tuple[dict, ...] = ()
    clue_triggers: int = 0
    strategist_applied: int = 0
    planned: set[str] = field(default_factory=set)
    run: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    api_errors: list[str] = field(default_factory=list)

    def wrap(self, client):
        return CountingClient(client, self.api_errors)

    def _keys(self, op: dict | None) -> list[str]:
        if op is None:
            return []
        return [op_key(req) for req in self.required if op_matches(req, op)]

    def on_strategist_trigger(self, trigger: dict) -> None:
        if trigger.get("trigger") == "clue":
            self.clue_triggers += 1
        elif trigger.get("trigger") == "goal_done":
            self.finished.update(self._keys(trigger.get("op")))

    def on_strategist_applied(self, goals: list[dict]) -> None:
        self.strategist_applied += 1
        for goal in goals:
            self.planned.update(self._keys(goal))

    def before_tick(
        self,
        w: WorldModel,
        m: Memory,
        *,
        state: str,
        reason: str,
        intents: list[dict] | None,
        policy: Policy,
        params: dict[str, float | int],
        knowledge: KnowledgeBase | None,
        acted_op: dict | None = None,
        plan_op: dict | None = None,
    ) -> None:
        if not intents or acted_op is None:
            return
        if state in OP_DRIVERS.get(acted_op.get("op", ""), ()):
            self.run.update(self._keys(acted_op))

    def failures(self) -> list[str]:
        out: list[str] = []
        if self.clue_triggers < 1:
            out.append("no clue trigger reached the strategist")
        for req in self.required:
            key = op_key(req)
            if key not in self.planned:
                out.append(f"strategist never planned {key}")
            if key not in self.run:
                out.append(f"{'/'.join(sorted(OP_DRIVERS[req['op']]))} never ran {key}")
            if key not in self.finished:
                out.append(f"{key} never finished")
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self) -> list[str]:
        keys = [op_key(r) for r in self.required]

        def listed(done: set[str]) -> str:
            return f"{sum(k in done for k in keys)}/{len(keys)}"

        return [
            f"clue triggers: {self.clue_triggers}",
            f"strategist applied: {self.strategist_applied}",
            f"planned {listed(self.planned)}, run {listed(self.run)}, finished {listed(self.finished)}",
            f"API errors: {len(self.api_errors)}",
        ]
