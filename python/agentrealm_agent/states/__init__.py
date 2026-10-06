"""Priority state machine (A5): guard / act / done and the list[Intent] test seam.

AI plans, state machine executes: reflexes act on what is happening now,
executors carry out the plan's top op, and the safe default explores safe
ground when there is no op (PLAN.md **Architecture**).
"""

from .base import PlayContext, State, StateOutcome, my_op, top_executor
from .dispatch import EXECUTORS, REFLEXES, STATES, dispatch
from .explore import ExploreState, explore_outcome, safe_default
from .gather import GatherState, gather_outcome
from .heal import HealState
from .boss import BossState
from .level import LevelState
from .solve import SolveState
from .travel import TravelState

__all__ = [
    "EXECUTORS",
    "ExploreState",
    "GatherState",
    "HealState",
    "BossState",
    "LevelState",
    "REFLEXES",
    "SolveState",
    "TravelState",
    "PlayContext",
    "STATES",
    "State",
    "StateOutcome",
    "dispatch",
    "explore_outcome",
    "gather_outcome",
    "my_op",
    "safe_default",
    "top_executor",
]
