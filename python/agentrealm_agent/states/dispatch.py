"""Pick the state to run this round trip and run its act (A5).

``STATES`` is the extension point: insert your state class here in priority
order (see ``docs/MAKE_IT_YOURS.md``). Per-state behavior is documented on
each class in this package and in ``docs/CHARACTER_AND_STATES.md``.
"""

from __future__ import annotations

from .. import idle_watchdog
from ..navigation import oscillation
from ..navigation.rejection import end_decision
from ..pathing import note_goto_reached
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .downed import DownedState
from .escape import EscapeState
from .explore import ExploreState
from .fight import FightState
from .flee import FleeState
from .gather import GatherState
from .heal import HealState
from .idle import IdleState
from .boss import BossState, sync_boss
from .level import LevelState
from .break_state import BreakState, OddBreakState
from .investigate import InvestigateState
from .loot import LootState
from .equip import EquipState
from ..shop import sync_shop
from .shop import ShopState
from .recover import RecoverState
from .retreat import RetreatState
from .solve import SolveState
from .sync import SyncState
from .travel import TravelState

# Priority order (PLAYABLE_AGENT_PLAN.md State machine). Sync and Downed are
# both priority 0 and never both act: each only waits. Escape, Retreat and
# Heal (A10) are priority 1; Fight (A23) slots in before Flee at 2; Recover
# (A11), Equip (A19), Loot (A20) and Shop (A21) are priority 3, in the plan's table order; Investigate
# (A30) and Solve (A39) are priority 4, above Gather (A22), Travel (A27), Boss
# (A38) and Level (A37) at 5. M8 economy states slot above Explore. Break for
# a plan op or stuck step 2 (A28) sits with Investigate; OddBreak, Break on an odd block
# (A31) sits below Level so curiosity never preempts Solve, Travel, Boss or Level.
STATES: tuple[State, ...] = (
    SyncState(),
    DownedState(),
    EscapeState(),
    RetreatState(),
    HealState(),
    FightState(),
    FleeState(),
    RecoverState(),
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
    OddBreakState(),
    ExploreState(),
    IdleState(),
)


def dispatch(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """Run the first state that is active and not done, or whose guard holds.

    Higher-priority guards always win; the active state keeps running past
    its own guard until its ``done`` holds. A state that runs (active or guard
    holds) but whose ``act`` sends no intent falls through to the next state (A44),
    unless it sets ``StateOutcome.wait``. Each call is one decision window:
    it ages what Step rejections taught the map (A14), and starts, ends or
    finishes the boss fight before any guard reads it (A38), and settles
    a Shop purchase whose gems were spent (A21). It also notes a policy
    ``goto`` the agent stands on, which is then satisfied (A16).

    Before any state runs, the oscillation guard checks whether the character
    is pacing between two cells, whichever states are doing it, and if so
    gives up the target it walks to; after the pick, ``note_move`` tells it
    which walk the move belongs to (``navigation/oscillation.py``, A15).

    The idle watchdog runs there too: after ``IDLE_REDIRECT_SECONDS`` with
    nothing productive it gives up the target and any plan ``wait``, and
    backs off the state that was idling (never Explore, Idle or a survival
    state, ``NEVER_BACKED_OFF``), which is then skipped until its backoff ends (``idle_watchdog.py``, A61).
    """
    m = ctx.memory
    m.nav_stuck.decision += 1
    yielded: list[str] = []
    if oscillation.check(m, world) is not None:
        yielded.append("oscillation: paced between two cells")
    if (idle := idle_watchdog.check(m, world, ctx.plan)) is not None:
        yielded.append(f"idle: {idle['state']} did nothing for {idle['ticks_idle']} ticks")
    note_goto_reached(world, m, ctx.policy)
    sync_boss(world, m, ctx.plan)
    sync_shop(world, m)
    try:
        outcome = _run_states(world, ctx, yielded)
    finally:
        end_decision(m.nav, world.tick)
    oscillation.note_move(m, outcome.intents, m.state)
    return outcome


def _run_states(world: WorldModel, ctx: PlayContext, yielded: list[str]) -> StateOutcome:
    """The first state, in ``STATES`` order, that runs and sends an intent or waits."""
    m = ctx.memory
    for state in STATES:
        if idle_watchdog.held_off(m, state.name, world.tick):
            continue  # redirected away from for idling (A61)
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
