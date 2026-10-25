"""Lou's considerations: why one action beats another (docs/v3/SPEC.md **Controller**).

Each returns the change an action makes to one thing Lou values, in rough
shares of a whole (a task's next step is 1, all our health is 1). The
controller adds them, each times its weight in ``WEIGHTS``. A new reason to
prefer an action is a new class here, never a branch in the loop.
"""

from __future__ import annotations

from agentrealm_adk import supplies

from adk3.core.interfaces import Action, Context
from adk3.realm.state import State, danger_at, hit_on_us, our_hit, threat_of

GEMS_WORTH_A_TASK_STEP = 25  # how many gems picked up are worth as much as a task's next step
LIFE_WORTH = 1.0  # a life picked up, against a task's next step


class TaskProgress:
    """How much the action advances the task under way."""

    name = "task"

    def score(self, a: Action, s: State, ctx: Context) -> float:
        return ctx.task.wants(a, s) if ctx.task is not None else 0.0


class Danger:
    """Danger left behind, less danger walked into: the share of our health the threats near could take.

    The strategist's ``risk`` sets how much it counts: at 0 fully, at 1 a quarter.
    """

    name = "danger"

    def score(self, a: Action, s: State, ctx: Context) -> float:
        at = a.data.get("at")
        if at is None:
            return 0.0
        return (danger_at(s, s.pos) - danger_at(s, at)) * (1.0 - 0.75 * ctx.risk)


class Fight:
    """A swing at a threat: its expected damage to them against theirs to us, so a fight we win scores and one we lose does not."""

    name = "fight"

    def score(self, a: Action, s: State, ctx: Context) -> float:
        e = a.data.get("npc")
        if a.kind != "attack" or e is None:
            return 0.0
        theirs = hit_on_us(s, e)
        edge = our_hit(s) / max(1.0, theirs)  # above 1, we trade hits in our favor
        hurt = 1.0 - s.health_frac
        return threat_of(s, e) * (min(edge, 2.0) - 1.0 - hurt + ctx.risk)


class Health:
    """Health back from a drink, worth more the more danger there is."""

    name = "health"

    def score(self, a: Action, s: State, ctx: Context) -> float:
        heal = a.data.get("heal", 0)
        if not heal:
            return 0.0
        return heal / max(1, s.max_health) * (1.0 + danger_at(s, s.pos)) * (1.0 - s.health_frac)


class Pickup:
    """What a supply beside us is worth: gems, a life, food while hurt; anything else a little, to learn it."""

    name = "pickup"

    def score(self, a: Action, s: State, ctx: Context) -> float:
        e = a.data.get("supply")
        if a.kind != "take" or e is None:
            return 0.0
        row = supplies.row(e.code)
        if row is not None and row.supply_class == "gem":
            return gem_count(e.code) / GEMS_WORTH_A_TASK_STEP
        if supplies.is_life(e.code):
            return LIFE_WORTH
        if supplies.eaten_on_pickup(e.code):
            return min(supplies.heal_of(e.code), s.max_health - s.health) / max(1, s.max_health)
        return 0.1


def gem_count(code: str) -> int:
    """Gems a gem supply adds: ``gem_cache_7`` seven, a plain ``gem`` one."""
    tail = code.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 1


ALL = [TaskProgress(), Danger(), Fight(), Health(), Pickup()]
WEIGHTS = {"task": 1.0, "danger": 2.0, "fight": 1.0, "health": 1.5, "pickup": 1.0}
