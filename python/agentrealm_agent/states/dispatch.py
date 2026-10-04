"""Pick the state to run this round trip and run its act (A5)."""

from __future__ import annotations

from ..navigation.rejection import end_decision
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .downed import DownedState
from .escape import EscapeState
from .explore import ExploreState
from .flee import FleeState
from .gather import GatherState
from .heal import HealState
from .idle import IdleState
from .investigate import InvestigateState
from .loot import LootState
from .recover import RecoverState
from .retreat import RetreatState
from .sync import SyncState
from .travel import TravelState

# Priority order (PLAYABLE_AGENT_PLAN.md State machine). Sync and Downed are
# both priority 0 and never both act: each only waits. Escape, Retreat and
# Heal (A10) are priority 1; Fight (A23) slots in before Flee at 2; Recover
# (A11) and Loot (A20) are priority 3, in the plan's table order, above Gather
# (A22) and Travel (A27) at 5. M8 economy states slot above Explore.
STATES: tuple[State, ...] = (
    SyncState(),
    DownedState(),
    EscapeState(),
    RetreatState(),
    HealState(),
    FleeState(),
    RecoverState(),
    LootState(),
    InvestigateState(),
    GatherState(),
    TravelState(),
    ExploreState(),
    IdleState(),
)


def dispatch(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """Run the first state that is active and not done, or whose guard holds.

    Higher-priority guards always win; the active state keeps running past
    its own guard until its ``done`` holds. Each call is one decision window:
    it ages what Step rejections taught the map (A14).
    """
    m = ctx.memory
    try:
        for state in STATES:
            active = state.name == m.state and not state.done(world, ctx)
            if active or state.guard(world, ctx):
                m.state = state.name
                return state.act(world, ctx)
        m.state = ""
        return StateOutcome(None, "no state")
    finally:
        end_decision(m.nav, world.tick)
