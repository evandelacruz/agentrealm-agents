"""Pick the state to run this round trip and run its act (A5)."""

from __future__ import annotations

from ..navigation.rejection import end_decision
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .downed import DownedState
from .explore import ExploreState
from .idle import IdleState
from .recover import RecoverState
from .sync import SyncState
from .travel import TravelState

# Priority order (PLAYABLE_AGENT_PLAN.md State machine). Sync and Downed are
# both priority 0 and never both act: each only waits. A9–A10 slot above Explore
# later; Recover (A11) is priority 3, Travel (A27) priority 5.
STATES: tuple[State, ...] = (
    SyncState(),
    DownedState(),
    RecoverState(),
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
