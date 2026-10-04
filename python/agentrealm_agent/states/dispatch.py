"""Pick the first state whose guard holds and run its act (A5)."""

from __future__ import annotations

from ..world import WorldModel
from .base import StateOutcome
from ..brain import PlayContext
from .downed import DownedState
from .explore import ExploreState
from .idle import IdleState
from .sync import SyncState

# Priority order (PLAYABLE_AGENT_PLAN.md). Survival states A9–A11 slot in above Explore later.
STATES = (
    DownedState(),
    SyncState(),
    ExploreState(),
    IdleState(),
)


def dispatch(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """First matching state wins; ages blocked tiles like the old decide wrapper."""
    m = ctx.memory
    for state in STATES:
        if state.guard(world, ctx):
            outcome = state.act(world, ctx)
            m.blocked = {p: n - 1 for p, n in m.blocked.items() if n > 1}
            return outcome
    m.blocked = {p: n - 1 for p, n in m.blocked.items() if n > 1}
    return StateOutcome(None, "no state", state="")
