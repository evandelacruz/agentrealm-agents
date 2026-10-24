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
shorter expiry. An ``until`` already met when the ``cannot`` comes (``gems
N`` with N held, ``already_met``) is no wait: the block holds to its expiry
and the caller asks once more, saying so. Where she stands, her health and the readiness text change
every step or hit, so no ``until`` names them. Anything else is dropped, and
the block holds to its expiry.

Whatever ``until`` says, a blocked goal is retried once its blocker can
have changed (A169, ``lift_changed``): the state the planner saw changed
(``seen``: the region she stands in, what she carries, whether her health
covers what is ahead on her risk function), or a walk given up for it may
be walked again (``Block.given_up``); the next walk asks again. A walk
given up holds until the ground round it changes, never on time alone
(A171, ``navigation.stuck.note_ground``), so while every walk given up
for a goal holds, her standing in another region is no change: the goal
stays blocked, and the planner is asked once for another way.

The call itself is the ADK's (``agentrealm_adk.llm``): the provider, the
call budget and backoff Gary's planner has, and its reply parsing.
Lorraine imports nothing of Gary's, his prompt included. In play the call
runs off the tick thread (``Planner.start``, ``Planner.finish``, A141), so a
slow or hung call never holds up her reflexes.
"""

from __future__ import annotations

import json
import threading
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
from agentrealm_adk.navigation.stuck import NavStuckMemory, is_backed_off
from agentrealm_adk.planner_reference import game_notes_text, reference_text
from agentrealm_adk.sellers import codes_with_sellers
from agentrealm_adk.supplies import sold_codes
from agentrealm_adk.targets import GoalOp
from agentrealm_adk.world import WorldModel

from . import readiness as r
from .act import refuse
from .goals import BLOCK_TICKS, Block, Blocks, Node, Reading

if TYPE_CHECKING:
    from agentrealm_adk.knowledge_base import KnowledgeBase

# The state lines a ``cannot`` may wait on (``watch_lines``), and the two with a number.
UNTIL_LINES = ("held", "levels_cleared", "sellers", "entrance_needs")
GEMS, TICKS = "gems", "ticks"
# The side of the square a region is (``seen``): a block is retried once she stands in another.
REGION_CELLS = 16

CONTRACT = """# The question

You answer one question about one goal of an Agent Realm character: how to work it now. Plan from the game reference above; where it and the measured facts disagree, trust the measured facts.

You are shown the goal, the state its readiness reads, the ops that can work it, and the op the goal tree would run, as an example of an op's shape. Answer with one JSON object and nothing else:

- {"op": {"op": "<one of the ops>", ...its fields}, "why": "<one line>"} to run that op now. Name a cell only by an x and y the state gives.
- given_up lists the targets a walk gave up on, with why and what lifts each: a tick, and the ground round it changing (cells seen there that were not, a block opened or broken, an occupant gone, a capability gained). A give-up is not "unreachable": it holds only until what lifts it comes, so a cannot that rests on one lapses with it. Never answer with an op that walks to one of them (its cell, or a buy whose every seller is one) while it is listed: nothing has changed there. Answer with another way instead: explore toward it so the ground round it is seen, another target (another entrance, another seller), or getting what opens the way.
- {"cannot": "<why no op can work it now, in game terms>", "until": "<what would let it be worked>"} when none can. The goal is then set aside, and the character works on what is under it, until then. until is one of: "held" (what is carried changes), "levels_cleared", "sellers" (a seller becomes known for another item), "entrance_needs" (the next level's entrance needs something else), "gems N" (N gems are held), or "ticks N" (N ticks pass, at most 3000). Anything else, or none, sets the goal aside for 3000 ticks."""


def node_state(node: Node, rd: Reading) -> dict[str, str]:
    """What the node's readiness reads, by name: its check, and the character's
    place, health, gems and what is carried; and the walk targets given up
    and still backed off, with why and until when (A169). Nothing of any other node."""
    w = rd.world
    where = f"map {w.map_id} at {w.pos[0]},{w.pos[1]}" if w.pos is not None else "unplaced"
    carried = sorted(s.code for s in w.held_supplies) + sorted(w.worn_codes.values())
    return {
        "readiness": rd.check(node).why,
        "where": where,
        "health": f"{w.health}/{w.max_health}",
        "gems": str(w.gems or 0),
        "held": ", ".join(carried) or "nothing",
        "given_up": "; ".join(rd.given_up) or "none",
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


def seen(w: WorldModel, kb: KnowledgeBase | None) -> tuple:
    """The state the planner saw, as a block retries on it (A169): the
    region she stands in, what she carries, and whether her health covers
    what is ahead, on her one risk function (``readiness.risk_budget``: the
    next level, and the hostiles that can reach her now). A hit or a heal
    that crosses neither line is no change, so it never re-asks."""
    where = (w.map_id, w.pos[0] // REGION_CELLS, w.pos[1] // REGION_CELLS) if w.pos is not None else None
    carried = tuple(sorted(s.code for s in w.held_supplies) + sorted(w.worn_codes.values()))
    level = r.Foes(tuple(r.level_foes(w, kb)), r.VALUABLE)
    here = r.Foes(tuple(r.foes_here(w, kb)), r.ORDINARY)
    return (where, carried, r.risk_budget(level, w, kb).ok, r.risk_budget(here, w, kb, now=True).ok)


def question_prefix(reference_sections: str = "") -> str:
    """The cached prefix: the game reference, the measured facts, the contract."""
    parts = [reference_text(reference_sections).strip()]
    notes = game_notes_text().strip()
    if notes:
        parts.append("# Measured facts (docs/GAME_NOTES.md)\n\n" + notes)
    parts.append(CONTRACT)
    return "\n\n".join(p for p in parts if p) + "\n"


def question(node: Node, rd: Reading, reference_sections: str = "", note: str = "") -> list[dict[str, Any]]:
    """The messages for the one question about ``node``: the game reference,
    the measured facts and the contract (cached), then the node."""
    own = node.op(node, rd)
    lines = [f"goal: {node.title}", "state:"]
    lines += [f"- {k}: {v}" for k, v in node_state(node, rd).items()]
    lines.append(f"ops: {', '.join(node.ops)}")
    lines.append(f"the tree's op: {json.dumps(own, sort_keys=True) if own else 'none'}")
    if note:
        lines.append(f"note: {note}")
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


def already_met(reply: Reply, w: WorldModel) -> bool:
    """A ``cannot`` waiting on what is true already: ``gems N`` with N held."""
    name, _, arg = reply.until.partition(" ")
    n = _count(arg)
    return reply.op is None and name == GEMS and n is not None and (w.gems or 0) >= n


ALREADY_MET = "your last answer was cannot until {until}, which is already true; answer again"


def answer(
    node: Node, reply: Reply, blocks: Blocks, rd: Reading, given_up: tuple[str, ...] = (), shown: tuple[str, ...] = ()
) -> GoalOp | None:
    """Carry a reply out on the tree: an op is returned to run; a ``cannot``
    blocks the node with its reason and what it waits on (``UNTIL_LINES``,
    ``gems N``, ``ticks N``); anything else it names is dropped. The block
    is retried as every block is (``lift_changed``): on the state the
    planner saw (``seen``), and once one of the walks ``given_up`` for the
    node, or one of the give-ups it was ``shown``, may be walked again (A171)."""
    if reply.op is not None:
        return reply.op
    name, _, arg = reply.until.partition(" ")
    now, ticks, named, was = rd.world.tick, BLOCK_TICKS, "", ""
    if name in UNTIL_LINES and not arg:
        named, was = name, watch_lines(rd.world, rd.kb)[name]
    elif name == GEMS and _count(arg) is not None and not already_met(reply, rd.world):
        named, was = GEMS, arg
    elif name == TICKS and _count(arg) is not None:
        ticks = min(BLOCK_TICKS, _count(arg))
    blocks.mark(node.key, reply.cannot, now, ticks, named=named, was=was, seen=seen(rd.world, rd.kb), given_up=given_up, shown=shown)
    return None


def lifts(block: Block, w: WorldModel, lines: dict[str, str]) -> bool:
    """Whether what ``block`` waits on has come: its line reads otherwise, or the gems are held."""
    if block.named == GEMS:
        return (w.gems or 0) >= int(block.was)
    return block.named in lines and lines[block.named] != block.was


def may_have_changed(block: Block, w: WorldModel, now: tuple, stuck: NavStuckMemory | None = None) -> bool:
    """Whether what blocked it can have changed (A169): a walk given up for
    it may be walked again (A171: its ground changed), or the state the
    planner saw is not ``now`` (``seen``). While every walk given up for it
    still holds, the region she stands in is no change: only what she
    carries or what her health covers is. A ``cannot`` lapses too once a
    give-up the planner was shown lifts: it may have rested on it."""
    if block.shown and (stuck is None or not all(is_backed_off(stuck, k, w.tick) for k in block.shown)):
        return True
    if not block.given_up:
        return bool(block.seen) and block.seen != now
    if stuck is None or not all(is_backed_off(stuck, k, w.tick) for k in block.given_up):
        return True
    return bool(block.seen) and block.seen[1:] != now[1:]


def lift_changed(
    blocks: Blocks, world: WorldModel, kb: KnowledgeBase | None, stuck: NavStuckMemory | None = None
) -> list[str]:
    """Lift every block whose awaited state has come, or whose blocker can
    have changed (``may_have_changed``, with her walks' give-ups in
    ``stuck``); the keys lifted."""
    lifted, lines = [], None
    now = seen(world, kb) if any(b.seen for b in blocks.values()) else ()
    for key, block in list(blocks.items()):
        if block.named:
            lines = lines if lines is not None else watch_lines(world, kb)
        if (block.named and lifts(block, world, lines)) or may_have_changed(block, world, now, stuck):
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

    def start(self, node: Node, rd: Reading, note: str = "") -> Call | None:
        """Send the question about ``node`` on its own thread (A141); None
        when no call is made (no planner, the budget spent, backing off).
        The messages are built and the budget charged here, on the caller's
        thread: the call's thread only waits on the provider."""
        if self.client is None or not self.ready():
            return None
        messages = question(node, rd, self.reference_sections, note)
        self.calls += 1
        self.budget.charge(self.clock(), messages)
        call = Call(node, messages, note)
        call.thread = threading.Thread(target=call.run, args=(self.client,), name="ladder-planner", daemon=True)
        call.thread.start()
        return call

    def finish(self, call: Call) -> Reply | None:
        """The reply of a call that is back (``Call.back``), or None when it
        failed: an error, an unreadable reply, or an op she cannot carry out
        as given (``act.refuse``), which is logged with why."""
        try:
            if call.error is not None:
                raise call.error
            self.budget.settle(call.usage)
            reply = read_reply(call.raw, call.node.ops)
            if reply.op is not None and (why := refuse(reply.op)):
                raise ValueError(f"refused {reply.op}: {why}")
        except Exception as e:  # network, HTTP status, refusal, an unreadable reply
            self.last_error = str(e) or type(e).__name__
            self.backoff.fail(self.clock())
            return None
        self.backoff.ok()
        return reply

    def ask(self, node: Node, rd: Reading, note: str = "") -> Reply | None:
        """Ask and wait for the reply: ``start``, then ``finish`` once it is back."""
        call = self.start(node, rd, note)
        if call is None:
            return None
        call.wait()
        return self.finish(call)


@dataclass
class Call:
    """One question out on its own thread: only the provider's call runs there (A141)."""

    node: Node
    messages: list[dict[str, Any]]
    note: str = ""
    raw: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    thread: threading.Thread | None = None

    def run(self, client: LLMClient) -> None:
        try:
            self.raw, self.usage = client.complete(self.messages)
        except Exception as e:  # read on the caller's thread, by ``Planner.finish``
            self.error = e

    @property
    def back(self) -> bool:
        """The call has returned, with a reply or an error."""
        return self.thread is not None and not self.thread.is_alive()

    def wait(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)
