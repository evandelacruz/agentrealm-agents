"""Lorraine's one planner question (A140): at the turn's goal, the op to run now, or ``cannot`` with why.

The planner is given one node of her goal tree (``ladder.goals``): its
title, the state its readiness predicate reads (``node_state``), the ops that
can work it, and the tree's own op for it as an example of the shape; and,
as the cached prefix, the game reference and the measured facts. It is asked
one thing, and answers with one JSON object::

    {"op": {"op": "gather_gems", "count": 12, "x": 40, "y": 7}, "why": "..."}
    {"cannot": "no seller of sandals is known", "until": "sellers"}

No stack and no stage prose: the tree is the plan, and the question is
about the node the walk reached.

A ``cannot`` marks the node blocked in ``goals.Blocks`` with its reason, for
``goals.BLOCK_TICKS``, and the next walk descends past it (A139). ``until``
names what would let it be worked, and only a line that changes when the
situation does (``UNTIL_LINES``, read by ``watch_lines``): what is carried,
the levels cleared, the items a seller is known for, the next entrance's
unmet need; ``gems N``, lifted once N gems are held; or ``ticks N``, a
shorter expiry. Where she stands, her health and the readiness text change
every step or hit, so they are never watched. Anything else is dropped, and
the block holds to its expiry (``lift_changed``).

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
from agentrealm_adk.sellers import codes_with_sellers
from agentrealm_adk.supplies import sold_codes
from agentrealm_adk.targets import GoalOp
from agentrealm_adk.world import WorldModel

from . import readiness as r
from .goals import BLOCK_TICKS, Block, Blocks, Node, Reading

if TYPE_CHECKING:
    from agentrealm_adk.knowledge_base import KnowledgeBase

# The state lines a ``cannot`` may wait on (``watch_lines``), and the two with a number.
UNTIL_LINES = ("held", "levels_cleared", "sellers", "entrance_needs")
GEMS, TICKS = "gems", "ticks"

CONTRACT = """# The question

You answer one question about one goal of an Agent Realm character: how to work it now. Plan from the game reference above; where it and the measured facts disagree, trust the measured facts.

You are shown the goal, the state its readiness reads, the ops that can work it, and the op the goal tree would run, as an example of an op's shape. Answer with one JSON object and nothing else:

- {"op": {"op": "<one of the ops>", ...its fields}, "why": "<one line>"} to run that op now. Name a cell only by an x and y the state gives.
- {"cannot": "<why no op can work it now, in game terms>", "until": "<what would let it be worked>"} when none can. The goal is then set aside, and the character works on what is under it, until then. until is one of: "held" (what is carried changes), "levels_cleared", "sellers" (a seller becomes known for another item), "entrance_needs" (the next level's entrance needs something else), "gems N" (N gems are held), or "ticks N" (N ticks pass, at most 3000). Anything else, or none, sets the goal aside for 3000 ticks."""


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


def watch_lines(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, str]:
    """The state lines a block may wait on: each changes only when the situation does."""
    carried = sorted(s.code for s in w.held_supplies) + sorted(w.worn_codes.values())
    return {
        "held": ", ".join(carried),
        "levels_cleared": ", ".join(str(n) for n in sorted(w.levels_cleared or ())),
        "sellers": ", ".join(sorted(codes_with_sellers(w, kb, sold_codes()))),
        "entrance_needs": r.need_of_next_level(w, kb) or "",
    }


def question_prefix(reference_sections: str = "") -> str:
    """The cached prefix: the game reference, the measured facts, the contract."""
    parts = [reference_text(reference_sections).strip()]
    notes = game_notes_text().strip()
    if notes:
        parts.append("# Measured facts (docs/GAME_NOTES.md)\n\n" + notes)
    parts.append(CONTRACT)
    return "\n\n".join(p for p in parts if p) + "\n"


def question(node: Node, rd: Reading, reference_sections: str = "") -> list[dict[str, Any]]:
    """The messages for the one question about ``node``: the game reference,
    the measured facts and the contract (cached), then the node."""
    own = node.op(node, rd)
    lines = [f"goal: {node.title}", "state:"]
    lines += [f"- {k}: {v}" for k, v in node_state(node, rd).items()]
    lines.append(f"ops: {', '.join(node.ops)}")
    lines.append(f"the tree's op: {json.dumps(own, sort_keys=True) if own else 'none'}")
    return [
        {"role": "system", "content": question_prefix(reference_sections), "cache": True},
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


def _count(text: str) -> int | None:
    try:
        n = int(text)
    except ValueError:
        return None
    return n if n > 0 else None


def answer(node: Node, reply: Reply, blocks: Blocks, rd: Reading) -> GoalOp | None:
    """Carry a reply out on the tree: an op is returned to run; a ``cannot``
    blocks the node with its reason and what it waits on (``UNTIL_LINES``,
    ``gems N``, ``ticks N``); anything else it names is dropped."""
    if reply.op is not None:
        return reply.op
    name, _, arg = reply.until.partition(" ")
    now, ticks, named, was = rd.world.tick, BLOCK_TICKS, "", ""
    if name in UNTIL_LINES and not arg:
        named, was = name, watch_lines(rd.world, rd.kb)[name]
    elif name == GEMS and _count(arg) is not None:
        named, was = GEMS, arg
    elif name == TICKS and _count(arg) is not None:
        ticks = min(BLOCK_TICKS, _count(arg))
    blocks.mark(node.key, reply.cannot, now, ticks, named=named, was=was)
    return None


def lifts(block: Block, w: WorldModel, lines: dict[str, str]) -> bool:
    """Whether what ``block`` waits on has come: its line reads otherwise, or the gems are held."""
    if block.named == GEMS:
        return (w.gems or 0) >= int(block.was)
    return block.named in lines and lines[block.named] != block.was


def lift_changed(blocks: Blocks, world: WorldModel, kb: KnowledgeBase | None) -> list[str]:
    """Lift every block whose awaited state has come; the keys lifted."""
    lifted, lines = [], None
    for key, block in list(blocks.items()):
        if not block.named:
            continue
        lines = lines if lines is not None else watch_lines(world, kb)
        if lifts(block, world, lines):
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
    _tokens: int = 0  # the resolved tokens_per_min

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

    def ready(self) -> bool:
        """A call may go out now: a planner, no backoff, budget left."""
        if self.client is None:
            return False
        now = self.clock()
        if not self._tokens:
            self._tokens = self.tokens_per_min or default_tokens_per_min(question_prefix(self.reference_sections))
        return not self.backoff.waiting(now) and not self.budget.limit_reached(now, self.calls_per_min, self._tokens)

    def ask(self, node: Node, rd: Reading) -> Reply | None:
        if self.client is None or not self.ready():
            return None
        messages = question(node, rd, self.reference_sections)
        now = self.clock()
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
