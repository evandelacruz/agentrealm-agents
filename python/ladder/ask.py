"""Lorraine's one planner question (A140): at the turn's goal, the op to run now, or ``cannot`` with why.

The planner is given one node of her goal tree (``ladder.goals``): its
title, the state its readiness predicate reads (``node_state``), the ops that
can work it, and the tree's own op for it as an example of the shape; and,
as the cached prefix, the game reference and the measured facts. It is asked
one thing, and answers with one JSON object::

    {"op": {"op": "gather_gems", "count": 12, "x": 40, "y": 7}, "why": "..."}
    {"cannot": "no seller of sandals is known", "until": "held"}

No stack and no stage prose: the tree is the plan, and the question is
about the node the walk reached.

A ``cannot`` marks the node blocked in ``goals.Blocks`` with its reason, for
``goals.BLOCK_TICKS``, and the next walk descends past it (A139). ``until``
names the part of the node's state whose change would let it be worked;
once that part reads otherwise the block lifts early (``lift_changed``).
With no part named, or one the state does not have, it is ``readiness``.

The call itself is the ADK's (``agentrealm_adk.llm``): the provider, the
call budget and backoff Gary's planner has, and its reply parsing.
Lorraine imports nothing of Gary's, his prompt included.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from agentrealm_adk.llm import (
    DEFAULT_CALLS_PER_MIN,
    Backoff,
    CallBudget,
    LLMClient,
    ProviderConfig,
    check_client,
    connect,
    default_tokens_per_min,
    env_number,
    parse_reply,
    planner_disabled_by_env,
)
from agentrealm_adk.planner_reference import game_notes_text, reference_text
from agentrealm_adk.targets import GoalOp
from agentrealm_adk.world import WorldModel

from .goals import Blocks, Node, Reading, find

if TYPE_CHECKING:
    from agentrealm_adk.knowledge_base import KnowledgeBase

# The part of a node's state a block watches when its reason names none.
DEFAULT_UNTIL = "readiness"

CONTRACT = """# The question

You answer one question about one goal of an Agent Realm character: how to work it now. Plan from the game reference above; where it and the measured facts disagree, trust the measured facts.

You are shown the goal, the state its readiness reads, the ops that can work it, and the op the goal tree would run, as an example of an op's shape. Answer with one JSON object and nothing else:

- {"op": {"op": "<one of the ops>", ...its fields}, "why": "<one line>"} to run that op now. Name a cell only by an x and y the state gives.
- {"cannot": "<why no op can work it now, in game terms>", "until": "<the state line whose change would let it be worked>"} when none can. The goal is then set aside, and the character works on what is under it, until that state line changes."""


def node_state(node: Node, rd: Reading) -> dict[str, str]:
    """What the node's readiness reads, by name: its check, and the character's
    place, health, gems and what is carried. Nothing of any other node."""
    w = rd.world
    where = f"map {w.map_id} at {w.pos[0]},{w.pos[1]}" if w.pos is not None else "unplaced"
    carried = sorted(s.code for s in w.held_supplies) + sorted(w.worn_codes.values())
    return {
        "readiness": rd.check(node).why,
        "where": where,
        "health": f"{w.health}/{w.max_health}",
        "gems": str(w.gems or 0),
        "held": ", ".join(carried) or "nothing",
    }


def question(node: Node, rd: Reading, reference_sections: str = "") -> list[dict[str, Any]]:
    """The messages for the one question about ``node``: the game reference,
    the measured facts and the contract (cached), then the node."""
    parts = [reference_text(reference_sections).strip()]
    notes = game_notes_text().strip()
    if notes:
        parts.append("# Measured facts (docs/GAME_NOTES.md)\n\n" + notes)
    parts.append(CONTRACT)
    own = node.op(node, rd)
    lines = [f"goal: {node.title}", "state:"]
    lines += [f"- {k}: {v}" for k, v in node_state(node, rd).items()]
    lines.append(f"ops: {', '.join(node.ops)}")
    lines.append(f"the tree's op: {json.dumps(own, sort_keys=True) if own else 'none'}")
    return [
        {"role": "system", "content": "\n\n".join(p for p in parts if p) + "\n", "cache": True},
        {"role": "user", "content": "\n".join(lines)},
    ]


@dataclass(frozen=True)
class Reply:
    """The answer: an op to run, or ``cannot`` with why and the state line it waits on."""

    op: GoalOp | None = None
    cannot: str = ""
    until: str = ""
    why: str = ""


def _answers(obj: dict[str, Any]) -> bool:
    return "op" in obj or "cannot" in obj


def read_reply(raw: str, ops: tuple[str, ...]) -> Reply:
    """The reply as an op among ``ops`` or a ``cannot``. Raises ``ValueError``
    for anything else: no JSON, neither key, or an op the node cannot run."""
    obj = parse_reply(raw, _answers)
    if not isinstance(obj, dict) or not _answers(obj):
        raise ValueError("reply has neither op nor cannot")
    why = str(obj.get("why") or "")
    if "cannot" in obj:
        reason = str(obj["cannot"] or "").strip()
        if not reason:
            raise ValueError("cannot without a reason")
        return Reply(cannot=reason, until=str(obj.get("until") or "").strip(), why=why)
    op = obj["op"]
    if isinstance(op, str):  # the op's fields beside it, not under it
        op = {k: v for k, v in obj.items() if k != "why"}
    if not isinstance(op, dict) or op.get("op") not in ops:
        raise ValueError(f"op {op!r} is not one of {', '.join(ops)}")
    return Reply(op=op, why=why)


def answer(node: Node, reply: Reply, blocks: Blocks, rd: Reading) -> GoalOp | None:
    """Carry a reply out on the tree: an op is returned to run; a ``cannot``
    blocks the node with its reason and the part of its state it waits on."""
    if reply.op is not None:
        return reply.op
    state = node_state(node, rd)
    named = reply.until if reply.until in state else DEFAULT_UNTIL
    blocks.mark(node.key, reply.cannot, rd.world.tick, named=named, was=state[named])
    return None


def lift_changed(blocks: Blocks, world: WorldModel, kb: KnowledgeBase | None, root: Node) -> list[str]:
    """Lift every block whose named state reads otherwise now; the keys lifted."""
    rd = Reading(world, kb)
    lifted = []
    for key, block in list(blocks.items()):
        try:
            node = find(key, root)
        except KeyError:
            continue
        if block.named and node_state(node, rd).get(block.named) != block.was:
            blocks.lift(key)
            lifted.append(key)
    return lifted


@dataclass
class Planner:
    """Asks the one question within Gary's call budget and backoff (``agentrealm_adk.llm``).

    ``ask`` returns None when no call is made (no planner, the budget spent,
    backing off) or the call failed; the caller then runs the tree's own op."""

    client: LLMClient | None = None
    provider: ProviderConfig | None = None
    calls_per_min: int = DEFAULT_CALLS_PER_MIN
    tokens_per_min: int = 0  # 0: room for one write of the prefix (llm.default_tokens_per_min)
    reference_sections: str = ""
    clock: Callable[[], float] = time.monotonic
    budget: CallBudget = field(default_factory=CallBudget)
    backoff: Backoff = field(default_factory=Backoff)
    calls: int = 0
    last_error: str = ""

    @classmethod
    def off(cls) -> Planner:
        """No planner: the tree's own op runs at every node."""
        return cls()

    @classmethod
    def from_env(cls) -> Planner:
        """The planner Gary's settings name (``llm.ProviderConfig``), off with
        ``AGENTREALM_NO_PLANNER``. Raises ``PlannerConfigError`` when it is on
        and cannot run."""
        if planner_disabled_by_env():
            return cls.off()
        provider = ProviderConfig.from_env()
        return cls(
            client=connect(provider),
            provider=provider,
            calls_per_min=int(env_number("AGENTREALM_PLANNER_CALLS_PER_MIN", DEFAULT_CALLS_PER_MIN)),
            tokens_per_min=int(env_number("AGENTREALM_PLANNER_TOKENS_PER_MIN", 0)),
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def check(self) -> Exception | None:
        """One call before play (``llm.check_client``): a refused key raises."""
        if self.client is None or self.provider is None:
            return None
        return check_client(self.client, self.provider)

    def ask(self, node: Node, rd: Reading) -> Reply | None:
        if self.client is None:
            return None
        messages = question(node, rd, self.reference_sections)
        now = self.clock()
        tokens = self.tokens_per_min or default_tokens_per_min(messages[0]["content"])
        if self.backoff.waiting(now) or self.budget.limit_reached(now, self.calls_per_min, tokens):
            return None
        self.calls += 1
        self.budget.charge(now, messages)
        try:
            raw, usage = self.client.complete(messages)
            self.budget.settle(usage)
            reply = read_reply(raw, node.ops)
        except Exception as e:  # network, HTTP status, refusal, an unreadable reply
            self.last_error = str(e) or type(e).__name__
            self.backoff.fail(self.clock())
            return None
        self.backoff.ok()
        return reply
