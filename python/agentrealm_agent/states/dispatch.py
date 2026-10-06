"""Pick the state to run this round trip and run its act (A5).

AI plans, state machine executes (PLAN.md **Architecture**). Two kinds of
state share one priority list:

- **Reflexes** act on what is happening now, whatever the plan says.
- **Executors** run only to carry out the plan's current top op; each
  guard asks ``my_op``. Explore comes last: the executor for
  ``explore_area``, and otherwise the **safe default**, which explores safe
  ground whenever nothing above it sent an intent.

So exactly one thing picks the movement target at any moment: a reflex, the
executor for the top op, or the safe default.

``STATES`` is the extension point: insert your state class here in priority
order (see ``docs/MAKE_IT_YOURS.md``). Per-state behavior is documented on
each class in this package and in ``docs/CHARACTER_AND_STATES.md``.
"""

from __future__ import annotations

from ..navigation import oscillation
from ..navigation.rejection import end_decision
from ..pathing import note_goto_reached
from ..plan import OP_STATE, PLAN_STALL_SECONDS, Plan
from ..memory import Memory
from ..shop import sync_shop
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome, top_executor
from .boss import BossState, sync_boss
from .break_state import BreakState
from .downed import DownedState
from .equip import EquipState
from .escape import EscapeState
from .explore import ExploreState
from .fight import FightState
from .flee import FleeState
from .gather import GatherState
from .heal import HealState
from .idle import IdleState
from .investigate import InvestigateState
from .level import LevelState
from .loot import LootState
from .pickup import PickupState
from .recover import RecoverState
from .retreat import RetreatState
from .shop import ShopState
from .solve import SolveState
from .sync import SyncState
from .travel import TravelState
from .wait import WaitState

# Act on what is happening now (PLAYABLE_AGENT_PLAN.md State machine). Sync
# and Downed only wait, the forced waits. Escape, Retreat and Heal (A10)
# are survival; Fight (A23) slots in before Flee; then Pickup in reach (A20)
# and Recover (A11).
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
)

# Run only for the plan's top op (``plan.OP_STATE``). At most one guards for
# an op, so their order matters only for the side jobs: Break also opens a
# block stuck escalation nominated (A15), and Break and Solve re-arm a
# weapon they swapped out. Explore is last: it is also the safe default.
EXECUTORS: tuple[State, ...] = (
    EquipState(),
    LootState(),
    ShopState(),
    InvestigateState(),
    BreakState(),
    SolveState(),
    GatherState(),
    TravelState(),
    BossState(),
    LevelState(),
    WaitState(),
    ExploreState(),
)

# The ``idle`` and ``wander`` policy kinds (M1); never guards for ``scripted``.
STATES: tuple[State, ...] = REFLEXES + EXECUTORS + (IdleState(),)


def dispatch(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """Run the first state that is active and not done, or whose guard holds.

    Higher-priority guards always win; the active state keeps running past
    its own guard until its ``done`` holds. A state that runs but sends no
    intent falls through to the next one (A44), unless it sets
    ``StateOutcome.wait``. Explore, last, is the safe default, so a scripted
    decision is never idle.

    Each call is one decision window. First it settles the plan: finished
    ops pop, and an op no state can carry out is dropped. It ages what Step
    rejections taught the map (A14), starts, ends or finishes the boss fight
    (A38), settles a Shop purchase (A21), and notes a policy ``goto`` stood
    on (A16). After the pick, the top op's stall clock runs unless its
    executor made progress (A34).

    The oscillation guard is a safety net: it checks whether the character
    is pacing between two cells, whichever states are doing it, and gives up
    the target it walks to (``navigation/oscillation.py``, A15).
    """
    m = ctx.memory
    m.nav_stuck.decision += 1
    yielded: list[str] = []
    if oscillation.check(m, world) is not None:
        yielded.append("oscillation: paced between two cells")
    note_goto_reached(world, m, ctx.policy)
    if ctx.plan is not None:
        _settle_plan(ctx.plan, world, m)
    sync_boss(world, m, ctx.plan)
    sync_shop(world, m)
    op = ctx.plan.current() if ctx.plan is not None else None
    owner = top_executor(ctx)
    try:
        outcome = _run_states(world, ctx, yielded)
    finally:
        end_decision(m.nav, world.tick)
    if op is not None and ctx.plan is not None and ctx.plan.current() is op:
        _note_op_progress(ctx.plan, world, m, op, owner, outcome)
    oscillation.note_move(m, outcome.intents, m.state)
    return outcome


def _settle_plan(plan: Plan, world: WorldModel, m: Memory) -> None:
    """Pop finished ops, and drop ops no state carries out (``hunt``, ``avoid``)."""
    plan.advance(world, m)
    while (op := plan.current()) is not None and OP_STATE.get(op["op"]) is None:
        plan.drop_current("no executor state", memory=m)
        plan.advance(world, m)


def _note_op_progress(plan: Plan, world: WorldModel, m: Memory, op: dict, owner: str | None, out: StateOutcome) -> None:
    """The executor moved the op forward, or its stall clock runs (A34).

    Any intent the executor sends for the op records it as acted on (A36);
    only progress resets the clock. A reflex holding the round leaves the
    clock alone; an op stalled for ``PLAN_STALL_SECONDS`` is dropped, so the
    stack never pins the agent.
    """
    if out.state == owner and out.intents:
        plan.acted = op
        if out.progress:
            plan.note_progress()
            return
    if out.state == owner and out.wait and out.progress:
        return
    if out.state != owner and any(out.state == s.name for s in REFLEXES):
        return
    if plan.note_stalled(world.tick):
        plan.drop_current(f"{owner}: no progress for {PLAN_STALL_SECONDS}s", memory=m)


def _run_states(world: WorldModel, ctx: PlayContext, yielded: list[str]) -> StateOutcome:
    """The first state, in ``STATES`` order, that runs and sends an intent or waits."""
    m = ctx.memory
    for state in STATES:
        active = state.name == m.state and not state.done(world, ctx)
        if not (active or state.guard(world, ctx)):
            continue
        outcome = state.act(world, ctx)
        if outcome.intents or outcome.wait:
            m.state = state.name
            outcome.yielded = yielded
            return outcome
        yielded.append(f"{state.name}: {outcome.reason}")
    m.state = ""
    reason = f"no state ({'; '.join(yielded)})" if yielded else "no state"
    return StateOutcome(None, reason, yielded=yielded)
