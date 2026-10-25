"""Lou's real-time play: proposers, and the one score the judge takes them by (docs/v3/SPEC.md **Engines**).

Each tick every proposer offers options for the moment:

- the skill's own ways to do its work (several places to work or explore, not one);
- the boss room's: fight the boss, or leave by a door before its clock runs out;
- combat's, when a threat can reach us: fight it, back off, drink, stand;
- the opportunities in view: a gem, food when hurt, a sign, a helper, an odd block, our death chest.

``score`` prices every option the same way, in gems: what it wins (an item
for sure, progress per step, field work per tick, a kill's drop) less what
it risks (health it is expected to cost, and the chance it kills us), from
one danger model (``adk3.realm.harm``) on one set of dice per engagement.
The judge (``adk3.core.engines.Judge``) takes the best and commits to it.
None of this calls a model: it plays the strategist's tactics and the levers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agentrealm_adk import supplies
from agentrealm_adk.pathing import flee_run
from agentrealm_adk.survival import attack_interval_ticks
from agentrealm_adk.world import Entity, Pos, chebyshev

from adk3.realm import actions, harm, travel, value
from adk3.realm.actions import Action
from adk3.realm.context import Ctx

from .opportunities import chances, near_first
from .orders import Do, Explore, Fight, GoTo, Order, Work

KILL_DROP = 0.3  # gems a kill drops before we have our own kills of a type: the Manual's ring-1 snotling rate (§16)
CUT_YIELD = 0.2  # gems a cut drops before we have our own cuts there: the Manual's ring-1 grass rate (§16)
LEVEL_WORTH = 300  # gems a level cleared is worth: an evening of field work (Manual §16), the whole point
SWITCH_MARGIN = 1.0  # gems: another option must beat the one under way by this much to replace it
BOSS_LIMIT_SECONDS = 90  # the shortest boss clock in Olympuff (Manual §11 Bosses and levels); the real one is not served
LEAVE_MARGIN_SECONDS = 15  # leave the boss room this long before the limit
CHOICES = 5  # places a skill offers to work or explore, spread apart
SPREAD = 4  # blocks between two places offered


@dataclass(frozen=True)
class Option:
    key: tuple  # what the judge commits to: a destination, a target, a pickup
    name: str
    course: harm.Course  # how it plays out, for the danger model
    action: Action | None  # what is sent this tick
    reward: float = 0.0  # gems won (or spent, below 0) for sure
    per_step: float = 0.0  # gems each step along the course is worth: progress
    kill_worth: float = 0.0  # gems a kill of the course's target is worth
    per_tick: float = 0.0  # gems each tick spent at the course's end is worth: work there

    def __str__(self) -> str:
        return self.name


# The score.

@dataclass
class Scorer:
    """One price for every option: reward less risk, in gems. Its dice are fixed for an engagement."""

    seed: int | None = None
    last_tick: int = -10**9
    cache: dict = field(default_factory=dict)

    def __call__(self, ctx: Ctx, o: Option) -> float:
        s = ctx.state
        if s.tick != self.last_tick:
            if s.tick - self.last_tick > 50:
                self.seed = s.tick  # a new engagement
            self.last_tick, self.cache = s.tick, {}
        period = harm.step_ticks(s)
        if harm.threats_near(s):
            odds = self.cache.get(o.course)
            if odds is None:
                odds = self.cache[o.course] = harm.odds(s, o.course, seed=self.seed, territory=ctx.memory.territory)
        else:
            steps = min(len(o.course.path), harm.HORIZON_TICKS // period)
            odds = harm.Odds(0.0, 0.0, 0.0, 0.0, float(steps))
        working = max(0.0, harm.HORIZON_TICKS - len(o.course.path) * period) if o.per_tick else 0.0
        price = Price(ctx)
        return (o.reward + odds.steps * o.per_step + odds.kill * o.kill_worth + working * o.per_tick
                - price.risk(odds))


class Price:
    """What a course's odds cost, in gems: health lost, and the chance of dying at what a death costs now."""

    def __init__(self, ctx: Ctx):
        self.per_health = value.health()
        self.death = value.death(ctx.levers, [c.code for c in ctx.state.carried])

    def risk(self, odds: harm.Odds) -> float:
        return odds.health_lost * self.per_health + odds.death * self.death


def step_worth(ctx: Ctx) -> float:
    """What one step of progress is worth: the time it saves, at what time is worth."""
    s = ctx.state
    return value.per_tick(ctx.levers, s.tick_hz) * harm.step_ticks(s) * s.tick_hz / harm.SCALE_HZ


# What the proposers share.

def walk_or_break(ctx: Ctx, path, why: str) -> Action | None:
    """Walk the path; when its next cell is a bush or tree we can break, break it first (Manual §11 Breaking blocks)."""
    if not path:
        return None
    s = ctx.state
    nxt = path[0]
    block = s.world.view.tiles.get(nxt)
    if block is not None and not s.world.walkable(nxt) and nxt in travel.breakable(s, ctx.memory):
        if not s.attack_ready:
            return actions.Stop("waiting to swing")
        return actions.UseOn(nxt[0], nxt[1], _arm_for(ctx, block), f"break the {block} in the way")
    return actions.Walk(tuple(path), why)


def _arm_for(ctx: Ctx, block: str) -> int | None:
    """The weapon to arm first to break ``block``, or None when the armed one does."""
    s = ctx.state
    weapon = travel.weapon_for(s, block)
    return None if weapon is None or any(c.id == weapon and c.where == "armed" for c in s.carried) else weapon


def spread(cells: list[Pos], take: int = CHOICES, apart: int = SPREAD) -> list[Pos]:
    """Up to ``take`` of ``cells``, in their order, each at least ``apart`` from those taken: real alternatives."""
    out: list[Pos] = []
    for p in cells:
        if all(chebyshev(p, q) >= apart for q in out):
            out.append(p)
            if len(out) == take:
                break
    return out


def stand() -> Option:
    return Option(("stand",), "stand", harm.Course(), actions.Stop("hold ground"))


# The skill's proposals.

class Skill:
    """The skill's order, offered as several ways to carry it out when it has a choice of place."""

    name = "skill"

    def propose(self, ctx: Ctx, order: Order | None) -> list[Option]:
        if isinstance(order, GoTo):
            return self.go(ctx, order)
        if isinstance(order, Explore):
            return self.explore(ctx, order)
        if isinstance(order, Work):
            return self.work(ctx, order)
        if isinstance(order, Do):
            return self.do(ctx, order)
        return []

    def go(self, ctx: Ctx, order: GoTo) -> list[Option]:
        path = travel.to_place(ctx, order.map_id, order.cell, door=order.door)
        if path is None:
            return []
        return [Option(("go", order.map_id, order.cell), order.why, harm.Course(tuple(path)),
                       walk_or_break(ctx, path, order.why), per_step=step_worth(ctx))]

    def explore(self, ctx: Ctx, order: Explore) -> list[Option]:
        s = ctx.state
        frontier = s.world.frontier() - {s.pos}
        toward = order.toward or s.pos
        out = []
        for cell in spread(sorted(frontier, key=lambda p: (chebyshev(p, toward), p))[:200]):
            path = travel.to_nearest(ctx, {cell})
            if path:
                out.append(Option(("explore", cell), f"{order.why} toward {cell[0]},{cell[1]}", harm.Course(tuple(path)),
                                  walk_or_break(ctx, path, order.why), per_step=step_worth(ctx)))
        return out

    def work(self, ctx: Ctx, order: Work) -> list[Option]:
        s = ctx.state
        cooldown = attack_interval_ticks(s.armed) * s.tick_hz / harm.SCALE_HZ
        out = []
        for cell in spread(list(order.cells)):
            rate = ctx.memory.gems_per_break(s.map_id, cell)
            per_tick = (CUT_YIELD if rate is None else rate) * ctx.levers.greed_weight / max(1.0, cooldown)
            if chebyshev(cell, s.pos) <= 1:
                act = actions.UseOn(cell[0], cell[1], _arm_for(ctx, "bush"), order.why) if s.attack_ready else None
                out.append(Option(("work", cell), f"{order.why} at {cell[0]},{cell[1]}", harm.Course(), act,
                                  per_tick=per_tick))
                continue
            path = travel.to_beside(ctx, cell)
            if path:
                out.append(Option(("work", cell), f"{order.why} at {cell[0]},{cell[1]}", harm.Course(tuple(path)),
                                  walk_or_break(ctx, path, order.why), per_tick=per_tick))
        return out

    def do(self, ctx: Ctx, order: Do) -> list[Option]:
        s = ctx.state
        if chebyshev(s.pos, order.at) <= order.reach:
            whole = step_worth(ctx) * harm.HORIZON_TICKS / harm.step_ticks(s)  # done now: a horizon of progress at once
            return [Option(("do", order.action.key), str(order.action), harm.Course(), order.action, reward=whole)]
        path = travel.to_beside(ctx, order.at, order.reach)
        if path is None:
            return []
        return [Option(("do", order.action.key), f"to {order.action}", harm.Course(tuple(path)),
                       walk_or_break(ctx, path, f"to {order.action}"), per_step=step_worth(ctx))]


# The boss room.

class Boss:
    """The boss fight on its clock (Manual §11 Bosses and levels): fight while it pays, leave by a door before the limit."""

    name = "boss"

    def __init__(self):
        self.began: float | None = None

    def propose(self, ctx: Ctx, order: Order | None) -> list[Option]:
        if not (isinstance(order, Fight) and order.boss):
            self.began = None
            return []
        s = ctx.state
        if self.began is None:
            self.began = time.time()
        limit = float(ctx.tactics.get("boss_seconds", BOSS_LIMIT_SECONDS))
        leave_at = limit - float(ctx.tactics.get("leave_with_seconds", LEAVE_MARGIN_SECONDS))
        out = []
        exit_path = travel.to_nearest(ctx, set(s.doors()), door=True)
        if exit_path:
            out.append(Option(("leave",), "leave the boss room", harm.Course(tuple(exit_path)),
                              actions.Walk(tuple(exit_path), "leave the boss room")))
        if time.time() - self.began >= leave_at:
            return out  # the clock: leaving is the only option left
        boss = next((e for e in s.npcs() if e.id == order.npc_id), None)
        if boss is not None:
            out.append(fight_option(ctx, boss, (), float(ctx.tactics.get("level_worth", LEVEL_WORTH))))
        return out


# Combat.

class Combat:
    """When a threat can reach us: fight it, back off, drink or stand, each priced on the danger model."""

    name = "combat"

    def propose(self, ctx: Ctx, order: Order | None) -> list[Option]:
        s = ctx.state
        threats = harm.threats_near(s)
        if not threats or (isinstance(order, Fight) and order.boss):
            return []
        out = [stand()]
        avoid = set(ctx.tactics.get("avoid") or [])
        way_on = next((o.course.path for o in Skill().propose(ctx, order) if o.course.path), ())
        for e in threats:
            if e.code not in avoid:
                out.append(fight_option(ctx, e, way_on, ctx.memory.gems_per_kill(e.code, KILL_DROP) * ctx.levers.greed_weight))
        away = travel.to_safe(ctx)
        if away:
            out.append(Option(("safe",), "back off to safe ground", harm.Course(tuple(away)),
                              walk_or_break(ctx, away, "to safe ground")))
        else:
            run = flee_run(s.world, threats, set(), _first_away(ctx, threats))
            if run:
                out.append(Option(("away",), "back off", harm.Course(tuple(run)), actions.Walk(tuple(run), "away")))
        drink = best_potion(ctx)
        if drink is not None:
            out.append(Option(("drink",), str(drink), harm.Course(heal=supplies.heal_of(drink.code)), drink,
                              reward=-value.item(drink.code)))
        return out


def fight_option(ctx: Ctx, e: Entity, then: tuple[Pos, ...], kill_worth: float) -> Option:
    """Close on ``e`` and swing at it until it dies (Manual §11 Combat: no swing lands from safe ground)."""
    s = ctx.state
    reach = s.world.attack_range or 1
    approach = tuple(travel.to_beside(ctx, e.pos, reach) or ()) if chebyshev(e.pos, s.pos) > reach else ()
    if approach:
        act = walk_or_break(ctx, approach, f"close on {e.code}")
    elif s.on_safe_ground:
        act = None
    else:
        act = actions.Swing(e.id, f"{e.code} {e.id}") if s.attack_ready else actions.Stop("between swings")
    return Option(("fight", e.id), f"fight {e.code}", harm.Course(approach, target=e.id, then=then), act,
                  kill_worth=kill_worth, per_step=step_worth(ctx))


def best_potion(ctx: Ctx) -> Action | None:
    s = ctx.state
    missing = s.max_health - s.health
    potions = [c for c in s.carried if supplies.is_potion(c.code)]
    if missing <= 0 or not potions:
        return None
    pick = min(potions, key=lambda c: abs(supplies.heal_of(c.code) - missing))
    return actions.Drink(pick.id, pick.where == "armed", pick.code)


def _first_away(ctx: Ctx, threats: list[Entity]) -> Pos:
    s = ctx.state
    options = s.world.open_neighbours(s.pos)
    if not options:
        return s.pos
    return max(options, key=lambda p: min(chebyshev(p, e.pos) for e in threats))


# Opportunities.

class Opportunities:
    """What is worth stopping for in view, each offered with its detour."""

    name = "opportunity"

    def propose(self, ctx: Ctx, order: Order | None) -> list[Option]:
        s = ctx.state
        out = []
        for c in near_first(ctx, chances(ctx)):
            if chebyshev(s.pos, c.at) <= c.reach:
                out.append(Option(("take", c.action.key), c.why, harm.Course(), c.action, reward=c.worth))
                continue
            path = travel.to_beside(ctx, c.at, c.reach)
            if path:
                out.append(Option(("take", c.action.key), c.why, harm.Course(tuple(path)),
                                  walk_or_break(ctx, path, c.why), reward=c.worth))
        return out


PROPOSERS = [Boss(), Combat(), Opportunities(), Skill()]
