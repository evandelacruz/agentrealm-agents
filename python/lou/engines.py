"""Lou's engines: real-time play, one per kind of situation (docs/v3/SPEC.md **Engines**).

Listed most urgent first; each tick the first whose situation holds acts.
None calls a model: they play the strategist's tactics (``ctx.tactics``) and
the agent's levers (``ctx.levers``) on what the moment shows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agentrealm_adk import supplies
from agentrealm_adk.pathing import flee_run
from agentrealm_adk.world import Entity, Pos, chebyshev

from adk3.realm import actions, harm, travel, value
from adk3.realm.actions import Action
from adk3.realm.context import Ctx

from .opportunities import chances, near_first
from .orders import Do, Explore, Fight, GoTo, Order, Work

KILL_DROP = 0.3  # gems a kill drops before we have our own kills of a type: the Manual's ring-1 snotling rate (§16)
SWITCH_MARGIN = 1.0  # gems: another option must beat the one under way by this much to replace it
BOSS_LIMIT_SECONDS = 90  # the shortest boss clock in Olympuff (Manual §11 Bosses and levels); the real one is not served
LEAVE_MARGIN_SECONDS = 15  # leave the boss room this long before the limit


# What the engines share.

def course_of(ctx: Ctx, order: Order | None) -> tuple[Pos, ...]:
    """The path the order would walk now: what going on with the skill means in a fight."""
    if isinstance(order, GoTo):
        path = travel.to_place(ctx, order.map_id, order.cell, door=order.door)
    elif isinstance(order, Explore):
        path = travel.to_unseen(ctx, order.toward)
    elif isinstance(order, Do):
        path = travel.to_beside(ctx, order.at, order.reach)
    elif isinstance(order, Work) and order.cells:
        path = work_path(ctx, order)
    else:
        path = None
    return tuple(path or ())


def walk_or_break(ctx: Ctx, path: list[Pos] | tuple[Pos, ...], why: str) -> Action | None:
    """Walk the path; when its next cell is a bush or tree we can break, break it first (Manual §11 Breaking blocks)."""
    if not path:
        return None
    s = ctx.state
    nxt = path[0]
    block = s.world.view.tiles.get(nxt)
    if block is not None and not s.world.walkable(nxt) and nxt in travel.breakable(s, ctx.memory):
        if not s.attack_ready:
            return actions.Stop("waiting to swing") if s.world.armed_code else None
        weapon = travel.weapon_for(s, block)
        arm = None if weapon is None or any(c.id == weapon and c.where == "armed" for c in s.carried) else weapon
        return actions.UseOn(nxt[0], nxt[1], arm, f"break the {block} in the way")
    return actions.Walk(tuple(path), why)


def do_at(ctx: Ctx, action: Action, at: Pos, reach: int, why: str) -> Action | None:
    """Do ``action`` from where we stand when ``at`` is in reach, else walk toward it."""
    if chebyshev(ctx.state.pos, at) <= reach:
        return action
    return walk_or_break(ctx, travel.to_beside(ctx, at, reach) or (), why)


# Combat: reward against risk.

@dataclass(frozen=True)
class Option:
    name: str
    course: harm.Course
    action: Action | None  # None: go on with the skill's own engine
    reward: float = 0.0  # gems it wins or spends for sure (a potion drunk is spent)
    kill_worth: float = 0.0  # gems a kill of its target is worth


class Combat:
    """When a threat can reach us: weigh every option as reward less risk, in gems, and take the best.

    Reward is the progress an option makes over the horizon (a fight goes on along the skill's way once
    its target is dead) and what a kill drops; risk is the health it is expected to cost and the chance
    it kills us. Within one engagement every option is weighed on the same dice, and the option under
    way is kept unless another beats it by ``SWITCH_MARGIN``, so noise never flips the choice."""

    name = "combat"

    def __init__(self):
        self._tick = -1
        self._choice: Option | None = None
        self._seed: int | None = None  # the engagement's dice

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        if isinstance(order, Fight) and order.boss:
            return False
        if not harm.threats_near(ctx.state):
            return False  # on safe ground too: stepping off it toward a threat is a choice to weigh like any other
        return self.choose(ctx, order).action is not None

    def act(self, ctx: Ctx, order: Order | None) -> Action | None:
        return self.choose(ctx, order).action

    def choose(self, ctx: Ctx, order: Order | None) -> Option:
        s = ctx.state
        if self._tick == s.tick and self._choice is not None:
            return self._choice
        if self._seed is None or s.tick - self._tick > 50:
            self._seed = s.tick  # a new engagement
        price = Price(ctx)
        per_step = value.per_tick(ctx.levers, s.tick_hz) * harm.step_ticks(s) * value_scale(s)

        def net(o: Option) -> float:
            odds = harm.odds(s, o.course, seed=self._seed, territory=ctx.memory.territory)
            return o.reward + odds.steps * per_step + odds.kill * o.kill_worth - price.risk(odds)

        scored = [(net(o), o) for o in self.options(ctx, order)]
        best_net, best = max(scored, key=lambda x: x[0])
        if self._choice is not None:
            kept = next(((n, o) for n, o in scored if o.name == self._choice.name), None)
            if kept is not None and kept[0] >= best_net - SWITCH_MARGIN:
                best = kept[1]
        self._choice, self._tick = best, s.tick
        return best

    def options(self, ctx: Ctx, order: Order | None) -> list[Option]:
        s = ctx.state
        ahead = course_of(ctx, order)
        out = [Option("go on", harm.Course(ahead), None)]
        avoid = set(ctx.tactics.get("avoid") or [])
        for e in harm.threats_near(s):
            if e.code in avoid:
                continue
            reach = s.world.attack_range or 1
            approach = tuple(travel.to_beside(ctx, e.pos, reach) or ()) if chebyshev(e.pos, s.pos) > reach else ()
            out.append(Option(f"fight {e.code}", harm.Course(approach, target=e.id, then=ahead),
                              self.fight(ctx, e, approach),
                              kill_worth=ctx.memory.gems_per_kill(e.code, KILL_DROP) * ctx.levers.greed_weight))
        away = travel.to_safe(ctx)
        if away:
            out.append(Option("back off to safe ground", harm.Course(tuple(away)), walk_or_break(ctx, away, "to safe ground")))
        else:
            run = flee_run(s.world, harm.threats_near(s), set(), _first_away(ctx))
            if run:
                out.append(Option("back off", harm.Course(tuple(run)), actions.Walk(tuple(run), "away")))
        drink = best_potion(ctx)
        if drink is not None:
            heal = supplies.heal_of(drink.code)
            out.append(Option(f"drink {drink.code}", harm.Course(heal=heal, path=ahead), drink, -value.item(drink.code)))
        out.append(Option("stand", harm.Course(), actions.Stop("hold ground")))
        return out

    def fight(self, ctx: Ctx, e: Entity, approach: tuple[Pos, ...]) -> Action | None:
        s = ctx.state
        if approach:
            return walk_or_break(ctx, approach, f"close on {e.code}")
        if s.on_safe_ground:
            return None  # no swing lands from a safe zone (Manual §11 Combat)
        return actions.Swing(e.id, f"{e.code} {e.id}") if s.attack_ready else actions.Stop("between swings")


class Price:
    """What a course's odds cost, in gems: health lost, and the chance of dying at what a death costs now."""

    def __init__(self, ctx: Ctx):
        carried = [c.code for c in ctx.state.carried]
        self.per_health = value.health()
        self.death = value.death(ctx.levers, carried)

    def risk(self, odds: harm.Odds) -> float:
        return odds.health_lost * self.per_health + odds.death * self.death


def value_scale(s) -> float:
    """Time in the model's ticks: the combat estimates run at 10 Hz whatever the world's rate."""
    return s.tick_hz / harm.SCALE_HZ


def best_potion(ctx: Ctx) -> Action | None:
    s = ctx.state
    missing = s.max_health - s.health
    potions = [c for c in s.carried if supplies.is_potion(c.code)]
    if missing <= 0 or not potions:
        return None
    pick = min(potions, key=lambda c: abs(supplies.heal_of(c.code) - missing))
    return actions.Drink(pick.id, pick.where == "armed", pick.code)


def _first_away(ctx: Ctx) -> Pos:
    s = ctx.state
    threats = harm.threats_near(s)
    options = s.world.open_neighbours(s.pos)
    if not options or not threats:
        return s.pos
    return max(options, key=lambda p: min(chebyshev(p, e.pos) for e in threats))


# The boss room.

class Boss:
    """The boss fight on its clock (Manual §11 Bosses and levels): fight while it pays, leave by a door before the limit."""

    name = "boss"

    def __init__(self):
        self.began: float | None = None

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        active = isinstance(order, Fight) and order.boss
        if not active:
            self.began = None
        return active

    def act(self, ctx: Ctx, order: Fight) -> Action | None:
        s = ctx.state
        if self.began is None:
            self.began = time.time()
        boss = next((e for e in s.npcs() if e.id == order.npc_id), None)
        limit = float(ctx.tactics.get("boss_seconds", BOSS_LIMIT_SECONDS))
        leave_at = limit - float(ctx.tactics.get("leave_with_seconds", LEAVE_MARGIN_SECONDS))
        odds = harm.odds(s, harm.Course(target=order.npc_id), territory=ctx.memory.territory) if boss is not None else None
        losing = odds is not None and odds.death > 0.5 and odds.kill < 0.2
        if time.time() - self.began >= leave_at or losing:
            exit_path = travel.to_nearest(ctx, set(s.doors()), door=True)
            if exit_path:
                return actions.Walk(tuple(exit_path), "leave the boss room")
        drink = best_potion(ctx)
        if drink is not None and s.health <= float(ctx.tactics.get("drink_below", s.max_health * ctx.levers.heal_below)):
            return drink
        if boss is None:
            return None
        return Combat().fight(ctx, boss, tuple(travel.to_beside(ctx, boss.pos, s.world.attack_range or 1) or ()))


# Opportunities.

class Opportunities:
    """Nothing threatens and something known is worth more than its detour: take it."""

    name = "opportunity"

    def __init__(self):
        self._tick = -1
        self._pick: Action | None = None

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        return self.pick(ctx) is not None

    def act(self, ctx: Ctx, order: Order | None) -> Action | None:
        return self.pick(ctx)

    def pick(self, ctx: Ctx) -> Action | None:
        s = ctx.state
        if self._tick == s.tick:
            return self._pick
        self._tick, self._pick = s.tick, None
        if harm.threats_near(s) and not s.on_safe_ground:
            return None
        per_step = value.per_tick(ctx.levers, s.tick_hz) * harm.step_ticks(s) * value_scale(s)
        price = Price(ctx)
        best, best_net = None, 0.0
        for c in near_first(ctx, chances(ctx)):
            if chebyshev(s.pos, c.at) <= c.reach:
                net, action = c.worth, c.action
            else:
                path = travel.to_beside(ctx, c.at, c.reach)
                if path is None:
                    continue
                danger = (price.risk(harm.odds(s, harm.Course(tuple(path)), territory=ctx.memory.territory))
                          if harm.threats_near(s) else 0.0)
                net, action = c.worth - len(path) * per_step - danger, walk_or_break(ctx, path, c.why)
            if net > best_net and action is not None:
                best, best_net = action, net
        self._pick = best
        return best


# The skill's own engines.

class Travel:
    """Walk where the skill goes, replanned every decision; break what is in the way when we can."""

    name = "travel"

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        return isinstance(order, (GoTo, Explore))

    def act(self, ctx: Ctx, order: GoTo | Explore) -> Action | None:
        if isinstance(order, GoTo):
            path = travel.to_place(ctx, order.map_id, order.cell, door=order.door)
        else:
            path = travel.to_unseen(ctx, order.toward)
        return walk_or_break(ctx, path or (), order.why)


class Interact:
    """Stand within reach and do the one thing the skill asks: take, read, talk, use, equip, empty a chest."""

    name = "interact"

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        return isinstance(order, Do)

    def act(self, ctx: Ctx, order: Do) -> Action | None:
        return do_at(ctx, order.action, order.at, order.reach, f"to {order.action}")


class WorkBlocks:
    """Break blocks for their drops, the best first; a weapon's break waits on its cooldown (Manual §11)."""

    name = "work"

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        return isinstance(order, Work)

    def act(self, ctx: Ctx, order: Work) -> Action | None:
        s = ctx.state
        beside = [p for p in order.cells if chebyshev(p, s.pos) <= 1]
        if beside:
            if not s.attack_ready:
                return None
            cell = beside[0]
            weapon = travel.weapon_for(s, "bush")  # a cutter: grass falls to what cuts bushes
            arm = None if weapon is None or any(c.id == weapon and c.where == "armed" for c in s.carried) else weapon
            return actions.UseOn(cell[0], cell[1], arm, order.why)
        return walk_or_break(ctx, work_path(ctx, order) or (), order.why)


def work_path(ctx: Ctx, order: Work) -> list[Pos] | None:
    """The cheapest way to stand beside one of the blocks to work: the cost grid prices ground near threats,
    so work goes where they are not."""
    s = ctx.state
    stands = {(x + dx, y + dy) for x, y in order.cells for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)}
    stands = {p for p in stands if p not in order.cells and (p not in s.world.view.tiles or s.world.walkable(p))}
    return travel.to_nearest(ctx, stands)


ALL = [Boss(), Combat(), Opportunities(), Travel(), Interact(), WorkBlocks()]
