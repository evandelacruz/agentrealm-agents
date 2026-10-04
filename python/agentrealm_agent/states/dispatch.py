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
    FightState(),
    FleeState(),
    RecoverState(),
    LootState(),
    InvestigateState(),
    GatherState(),
    TravelState(),
    ExploreState(),
    IdleState(),
)


def _sends_intent(outcome: StateOutcome) -> bool:
    return bool(outcome.intents)


def _falls_through(state: State, outcome: StateOutcome, ctx: PlayContext) -> bool:
    """True when a guard claim with no intent should yield to lower states (A44)."""
    if _sends_intent(outcome):
        return False
    if state.name in ("Sync", "Downed", "Idle", "Flee"):
        return False
    if state.name == "Heal":
        return "yield to Explore" in outcome.reason
    if state.name == "Recover" and outcome.reason.startswith("open chest"):
        return False
    return True


def dispatch(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """Run the first state that is active and not done, or whose guard holds.

    Higher-priority guards always win; the active state keeps running past
    its own guard until its ``done`` holds. When a state's ``guard`` holds
    but ``act`` sends no intent, dispatch falls through to the next state
    (A44) so the agent cannot freeze. Each call is one decision window:
    it ages what Step rejections taught the map (A14).
    """
    m = ctx.memory
    try:
        for state in STATES:
            active = state.name == m.state and not state.done(world, ctx)
            if not (active or state.guard(world, ctx)):
                continue
            outcome = state.act(world, ctx)
            if _sends_intent(outcome):
                m.state = state.name
                return outcome
            # Hysteresis: an active state whose guard released still owns the round.
            if active and not state.guard(world, ctx):
                m.state = state.name
                return outcome
            if _falls_through(state, outcome, ctx):
                continue
            m.state = state.name
            return outcome
        m.state = ""
        return StateOutcome(None, "no state")
    finally:
        end_decision(m.nav, world.tick)
