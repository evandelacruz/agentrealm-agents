"""Pick the state to run this round trip and run its act (A5)."""

from __future__ import annotations

from ..navigation.rejection import end_decision
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
from .investigate import InvestigateState
from .loot import LootState
from .shop import ShopState
from .recover import RecoverState
from .retreat import RetreatState
from .solve import SolveState
from .sync import SyncState
from .travel import TravelState

# Priority order (PLAYABLE_AGENT_PLAN.md State machine). Sync and Downed are
# both priority 0 and never both act: each only waits. Escape, Retreat and
# Heal (A10) are priority 1; Fight (A23) slots in before Flee at 2; Recover
# (A11), Loot (A20) and Shop (A21) are priority 3, in the plan's table order; Investigate
# (A30) and Solve (A39) are priority 4, above Gather (A22), Travel (A27), Boss
# (A38) and Level (A37) at 5. M8 economy states slot above Explore.
STATES: tuple[State, ...] = (
    SyncState(),
    DownedState(),
    EscapeState(),
    RetreatState(),
    HealState(),
    FightState(),
    FleeState(),
    RecoverState(),
    LootState(),
    ShopState(),
    InvestigateState(),
    SolveState(),
    GatherState(),
    TravelState(),
    BossState(),
    LevelState(),
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
    finishes the boss fight before any guard reads it (A38).
    """
    m = ctx.memory
    yielded: list[str] = []
    sync_boss(world, m, ctx.plan)
    try:
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
    finally:
        end_decision(m.nav, world.tick)
