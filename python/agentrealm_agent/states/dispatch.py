"""Pick the state to run this round trip and run its act (A5).

AI plans, state machine executes (PLAN.md **Architecture**). Two kinds of
state share one priority list:

- **Reflexes** act on what is happening now, whatever the plan says.
- **Executors** run only to carry out the plan's current top op; each
  guard asks ``my_op``. An executor keeps its op's walk: obstructed, it
  clears the way or asks the planner and holds (``way.clear_the_way``,
  A120). Explore comes last: the executor for ``explore_area``, and with no
  op the **safe default**, which explores safe ground; under any other op
  it holds.

So exactly one thing picks the movement target at any moment: a reflex, the
executor for the top op, or the safe default.

``STATES`` is the extension point: insert your state class here in priority
order (see ``docs/MAKE_IT_YOURS.md``). Per-state behavior is documented on
each class in this package and in ``docs/CHARACTER_AND_STATES.md``.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TypeVar

from ..navigation import oscillation
from ..navigation.rejection import end_decision
from ..engagement import sync_engagement
from ..equip import note_last_weapon
from ..healing import note_heal_window
from ..pathing import note_goto_reached
from ..plan import OP_STATE, PLAN_STALL_SECONDS, Plan
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..shop import sync_shop
from ..stalled import remember as remember_stalled, retry_basis
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome, top_executor
from .boss import BossState, sync_boss
from .break_state import BreakState
from .detour import DetourState
from .downed import DownedState
from .equip import EquipState
from .escape import EscapeState
from .explore import ExploreState
from .fight import FightState, held_target
from .flee import FleeState
from .gather import GatherState
from .greet import GreetState
from .heal import HealState
from .idle import IdleState
from .investigate import InvestigateState
from .level import LevelState
from .loot import LootState
from .park import ParkState
from .pickup import PickupState
from .recover import RecoverState
from .retreat import RetreatState
from .shop import ShopState
from .solve import SolveState
from .sync import SyncState
from .travel import TravelState
from .use_item import UseItemState
from .wait import WaitState
from .way import break_due

# Act on what is happening now (PLAYABLE_AGENT_PLAN.md State machine). Sync
# and Downed only wait, the forced waits. Escape, Retreat and Heal (A10)
# are survival; Fight (A23) slots in before Flee; then Pickup in reach (A20),
# Recover (A11), Detour (A71, A73) to a valuable in view worth the steps it adds,
# Greet (A65), a one-tick hello to an NPC in sight, and UseItem (A108), which
# applies a bigger chest once held and is also the ``use_item`` op's executor.
REFLEXES: tuple[State, ...] = (
    SyncState(),
    DownedState(),
    EscapeState(),
    RetreatState(),
    HealState(),
    FightState(),
    FleeState(),
    PickupState(),
    RecoverState(),
    DetourState(),
    GreetState(),
    UseItemState(),
)

# Run only for the plan's top op (``plan.OP_STATE``). At most one guards for
# an op, so their order matters only for the side jobs. Break sits below the
# walkers because it also opens the block a walk's stuck escalation nominated
# (A15): the walker yields at step 2 and Break acts in the same decision.
# Break and Solve re-arm a weapon they swapped out. Explore is last: it is
# also the safe default with no op, and holds under any other (A120).
EXECUTORS: tuple[State, ...] = (
    EquipState(),
    LootState(),
    ShopState(),
    InvestigateState(),
    SolveState(),
    GatherState(),
    TravelState(),
    BossState(),
    LevelState(),
    WaitState(),
    BreakState(),
    ExploreState(),
)

# The ``idle`` and ``wander`` policy kinds (M1); never guards for ``scripted``.
STATES: tuple[State, ...] = REFLEXES + EXECUTORS + (IdleState(),)

# The runner's park phase, after the run ends (A66): the survival reflexes,
# then Park walking to safe ground. No plan op runs, and nothing else.
SURVIVAL = ("Sync", "Downed", "Escape", "Retreat", "Heal", "Fight", "Flee")
PARK_STATES: tuple[State, ...] = tuple(s for s in REFLEXES if s.name in SURVIVAL) + (ParkState(),)

# The runner's held-queue probe (A64): the states that can answer with a
# reflex, so a poll while a queue runs skips every executor's search. Greet
# and UseItem are left out: a hello (A65) and a chest wait for a decision window. Boss is the one
# executor whose fight moves are reflexes, so it comes last and runs only
# once a boss fight is engaged (``Memory.boss``).
_BOSS = next(s for s in EXECUTORS if s.name == "Boss")
PROBE_STATES: tuple[State, ...] = tuple(s for s in REFLEXES if s.name not in (GreetState.name, UseItemState.name)) + (_BOSS,)
# Memory the held-queue probe puts back even when its reflex is sent: the
# navigation learnings and stuck attempts (only a real decision window ages or
# escalates them, A14, A15), the boss fight (it belongs to the stack, A38), and
# Greet's tries (its hello waits for a decision window, A65).
PROBE_NEVER_KEEPS = ("nav", "nav_stuck", "boss", "greetings")

D = TypeVar("D")


def probe_held_queue(
    m: Memory,
    rng: random.Random,
    plan: Plan,
    decide: Callable[[], D],
    sends: Callable[[D], bool],
) -> D | None:
    """The held-queue probe (A64): ``decide`` run with ``PROBE_STATES`` while a
    queue is held; its answer when ``sends`` says it is sent, else None.

    The probe has no side effects on memory unless its answer is sent:
    memory is snapshot before it and put back whole when nothing is sent
    (its search caches aside, ``memory.SEARCH_CACHES``), and so are the rng
    and the goal stack. A pick, a try, a commitment or a re-arm made for an
    answer that is not sent never happened (A71, A24, A22, A77): a ``Use``
    the probe only decided spends no try. The goal stack is put back even
    when the answer is sent: reflexes never consume its ops (A34). So is
    ``PROBE_NEVER_KEEPS``.
    """
    saved = m.snapshot()
    saved_rng = rng.getstate()
    saved_plan = plan.snapshot()
    try:
        d = decide()
    finally:
        plan.restore(saved_plan)
        m.restore(saved, PROBE_NEVER_KEEPS)
    if sends(d):
        return d
    m.restore(saved)
    rng.setstate(saved_rng)
    return None


def dispatch(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """Run the first state that is active and not done, or whose guard holds.

    Higher-priority guards always win; the active state keeps running past
    its own guard until its ``done`` holds. A state that runs but sends no
    intent falls through to the next one (A44), unless it sets
    ``StateOutcome.wait``. Explore, last, is the safe default with no op, so
    a scripted decision with no op is never idle; under an op it holds for
    that op's executor (A120).

    Each call is one decision window. First it settles the plan: finished
    ops pop, and an op no state can carry out is dropped. It ages what Step
    rejections taught the map (A14), starts, ends or finishes the boss fight
    (A38), settles a Shop purchase (A21), brings the fight-or-flee decision
    up to date (A94, ``engagement.sync_engagement``; a fight goes on while
    Fight has a target in it it can reach, A113), and notes a policy
    ``goto`` stood on (A16). After the pick, the top op's stall clock runs unless its
    executor made progress (A34).

    The oscillation guard is a safety net: it checks whether the character
    is pacing between two cells, whichever states are doing it, and gives up
    every walk that paced (``navigation/oscillation.py``, A15, A90), and the
    top op too when its executor paced with a step that was no walk: it
    stalled, so it is remembered and moved below as A104 does
    (``_stall_paced_op``).
    """
    m = ctx.memory
    m.nav_stuck.decision += 1
    yielded: list[str] = []
    paced = oscillation.check(m, world)
    if paced is not None:
        yielded.append("oscillation: paced between two cells")
    note_goto_reached(world, m, ctx.policy)
    note_heal_window(m, world)
    note_last_weapon(m, world)
    sync_shop(world, m, ctx.plan)  # before the plan settles: a Take gone stale has paid nothing (A112)
    if ctx.plan is not None:
        paced_op = ctx.plan.current()  # the op on top while it paced, before settling pops any
        _settle_plan(ctx.plan, world, m)
        if paced is not None and paced_op is not None:
            _stall_paced_op(ctx.plan, paced_op, paced, world, m, ctx.knowledge)
    sync_boss(world, m, ctx.plan)
    if ctx.policy.kind == "scripted":
        sync_engagement(world, m, ctx.policy, ctx.params, lambda e: held_target(world, ctx, e))
    op = ctx.plan.current() if ctx.plan is not None else None
    owner = top_executor(ctx)
    try:
        outcome = _run_states(world, ctx, yielded)
    finally:
        end_decision(m.nav, world.tick)
    if ctx.probe:
        return outcome  # not a decision window: no op's stall clock runs (A64)
    if op is not None and ctx.plan is not None and ctx.plan.current() is op:
        _note_op_progress(ctx.plan, world, m, op, owner, outcome, knowledge=ctx.knowledge)
    oscillation.note_move(m, world, outcome.intents, m.state)
    return outcome


def _settle_plan(plan: Plan, world: WorldModel, m: Memory) -> None:
    """Pop finished ops, and drop ops no state carries out (``hunt``, ``avoid``)."""
    plan.advance(world, m)
    while (op := plan.current()) is not None and OP_STATE.get(op["op"]) is None:
        plan.drop_current("no executor state", memory=m)
        plan.advance(world, m)


def _stall_paced_op(
    plan: Plan, op: dict, paced: dict, world: WorldModel, m: Memory, knowledge: KnowledgeBase | None
) -> None:
    """The top op's executor paced with a step that was no walk: the op stalled (A103, A104, A90).

    The guard gave up every walk that paced, and every walk keeps off a
    target given up, so an executor whose paced moves were all walks next
    picks another target, leaves the stuck ladder's step, or drops its op by
    its own rule. A step that was no walk gave nothing up, and each one
    resets the op's stall clock while it goes nowhere. So the op is treated
    as stalled at once: remembered in ``Memory.stalled_ops`` with why and
    what was known then, and moved below the next op that can make
    progress, or dropped when none can (``Plan.demote_current``). The why
    names the executor, the cells and the other pacers.

    ``op`` is the op that was on top while the character paced. Settling the
    plan may have popped it since (done, or no executor); then nothing moves,
    so the next op never pays for pacing it took no part in.
    """
    owner = OP_STATE.get(op["op"])
    if plan.current() is not op or owner is None or owner not in paced.get("unwalked", ()) or paced.get("escape"):
        return
    others = [s for s in paced["states"] if s != owner]
    cells = " and ".join(f"{x},{y}" for x, y in paced["cells"])
    with_whom = f" with {', '.join(others)}" if others else ""
    paced["op"] = dict(op)
    why = f"{owner} paced between {cells}{with_whom}: no way forward"
    remember_stalled(m.stalled_ops, op, why, world.tick, retry_basis(world, knowledge), world.map_id)
    plan.demote_current(why, memory=m)


def _note_op_progress(
    plan: Plan,
    world: WorldModel,
    m: Memory,
    op: dict,
    owner: str | None,
    out: StateOutcome,
    *,
    knowledge: KnowledgeBase | None = None,
) -> None:
    """The executor moved the op forward, or its stall clock runs (A34).

    Any intent the executor sends for the op records it as acted on (A36);
    only progress resets the clock, and so does Break working the owner's
    stuck escalation. A reflex holding the round leaves the clock alone. An op
    stalled for ``PLAN_STALL_SECONDS`` is remembered as stalled, with why and
    what was known then, and moved below the next op that can make progress
    (dropped when none can), so the stack never pins the agent (A104).
    """
    if out.state == owner and out.intents:
        m.no_way_on = None  # the executor acts again: a later ask from this cell is new (A120)
        plan.acted = op
        if out.progress:
            plan.note_progress()
            return
    if out.state == owner and out.wait and out.progress:
        return
    if out.state != owner and any(out.state == s.name for s in REFLEXES):
        return
    if out.state == BreakState.name != owner and out.intents and break_due(world, m, owner):
        # Break opening the block the owner's own stuck walk nominated (A15):
        # a step toward it is the walk's progress, and a Use try leaves the
        # clock alone, so the op is not dropped mid-break.
        if out.progress:
            plan.stalled_since_tick = None
        return
    if plan.note_stalled(world.tick):
        why = f"{owner}: no progress for {PLAN_STALL_SECONDS}s"
        remember_stalled(m.stalled_ops, op, why, world.tick, retry_basis(world, knowledge), world.map_id)
        plan.demote_current(why, memory=m)


def _states(ctx: PlayContext) -> tuple[State, ...]:
    """The states this call may run: ``PARK_STATES`` in the park phase (A66),
    else ``PROBE_STATES`` in the held-queue probe (A64), else all of ``STATES``."""
    m = ctx.memory
    if m.parking:
        return PARK_STATES  # Park walks with ``retreat_step``, a reflex: the probe keeps it
    if ctx.probe:
        return PROBE_STATES if m.boss is not None else PROBE_STATES[:-1]
    return STATES


def _run_states(world: WorldModel, ctx: PlayContext, yielded: list[str]) -> StateOutcome:
    """The first state, in ``_states`` order, that runs and sends an intent or
    waits.

    In the held-queue probe, when none does, ``Memory.state`` stays the state
    that sent the held queue (A64).
    """
    m = ctx.memory
    for state in _states(ctx):
        if oscillation.benched(m, world, state.name):
            continue  # it lost a flip with no walk (A136)
        active = state.name == m.state and not state.done(world, ctx)
        if not (active or state.guard(world, ctx)):
            continue
        outcome = state.act(world, ctx)
        if outcome.intents or outcome.wait:
            m.state = state.name
            outcome.yielded = yielded
            return outcome
        yielded.append(f"{state.name}: {outcome.reason}")
    if not ctx.probe:
        m.state = ""
    reason = f"no state ({'; '.join(yielded)})" if yielded else "no state"
    return StateOutcome(None, reason, yielded=yielded)
