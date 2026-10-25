"""Lou's reactions: real-time play in fixed priority (docs/v3/SPEC.md **Reactions**).

Each tick the first whose situation holds acts:

1. ``Combat``, when a hostile can reach us: an engagement, planned once and carried through.
2. ``Boss``, in a boss room: the fight on its clock.
3. ``Opportunities``, something in view worth its detour.
4. The approach's method: ``Travel``, ``Interact``, ``WorkBlocks``.

None calls a model. Each plays the running approach's tactics (``ctx.tactics``)
and the levers (``ctx.levers``) on what the moment shows.
"""

from __future__ import annotations

import math
import random
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
CLOSE_CALL = 1.0  # gems: options this close to the best are rolled for, not taken by strict order
BOSS_LIMIT_SECONDS = 90  # the shortest boss clock in Olympuff (Manual §11 Bosses and levels); the real one is not served
LEAVE_MARGIN_SECONDS = 15  # leave the boss room this long before the limit


# What the reactions share.

def course_of(ctx: Ctx, order: Order | None) -> tuple[Pos, ...]:
    """The path the approach's method would walk now: what going on means in a fight."""
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


def walk_or_break(ctx: Ctx, path, why: str) -> Action | None:
    """Walk the path; when its next cell is a bush or tree we can break, break it first (Manual §11 Breaking blocks)."""
    if not path:
        return None
    s = ctx.state
    nxt = path[0]
    block = s.world.view.tiles.get(nxt)
    if block is not None and not s.world.walkable(nxt) and nxt in travel.breakable(s, ctx.memory):
        if not s.attack_ready:
            return actions.Stop("waiting to swing") if s.world.armed_code else None
        return actions.UseOn(nxt[0], nxt[1], arm_for(ctx, block), f"break the {block} in the way")
    return actions.Walk(tuple(path), why)


def arm_for(ctx: Ctx, block: str) -> int | None:
    """The weapon to arm first to break ``block``, or None when the armed one does."""
    s = ctx.state
    weapon = travel.weapon_for(s, block)
    return None if weapon is None or any(c.id == weapon and c.where == "armed" for c in s.carried) else weapon


def do_at(ctx: Ctx, action: Action, at: Pos, reach: int, why: str) -> Action | None:
    """Do ``action`` from where we stand when ``at`` is in reach, else walk toward it."""
    if chebyshev(ctx.state.pos, at) <= reach:
        return action
    return walk_or_break(ctx, travel.to_beside(ctx, at, reach) or (), why)


def work_path(ctx: Ctx, order: Work) -> list[Pos] | None:
    """The cheapest way to stand beside one of the blocks to work."""
    s = ctx.state
    stands = {(x + dx, y + dy) for x, y in order.cells for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)}
    stands = {p for p in stands if p not in order.cells and (p not in s.world.view.tiles or s.world.walkable(p))}
    return travel.to_nearest(ctx, stands)


def best_potion(ctx: Ctx) -> Action | None:
    s = ctx.state
    missing = s.max_health - s.health
    potions = [c for c in s.carried if supplies.is_potion(c.code)]
    if missing <= 0 or not potions:
        return None
    pick = min(potions, key=lambda c: abs(supplies.heal_of(c.code) - missing))
    return actions.Drink(pick.id, pick.where == "armed", pick.code)


def bar(ctx: Ctx, foe: Entity | None) -> float:
    """The health at which a fight stops being pushed (and a potion is drunk): the tactics' bar for this
    hostile type, else their general bar, else the ``heal_below`` lever."""
    t = ctx.tactics
    per_type = (t.get("bars") or {}).get(foe.code) if foe is not None else None
    if per_type is not None:
        return float(per_type)
    if "bar" in t:
        return float(t["bar"])
    return ctx.levers.heal_below * ctx.state.max_health


# Combat: an engagement, planned once.

@dataclass(frozen=True)
class Option:
    name: str
    kind: str  # go_on | fight | back_off | hold | drink
    course: harm.Course
    reward: float = 0.0  # gems won (or spent, below 0) for sure
    kill_worth: float = 0.0
    target: Entity | None = None


@dataclass
class Engagement:
    kind: str
    target: Entity | None
    bar: float
    began: int  # tick
    seed: int
    hits_at_start: int  # our health when it began
    why: str
    approach: object = None  # the approach it was planned for


class Combat:
    """When a hostile can reach us, plan an engagement and carry it through (docs/v3/SPEC.md **Reactions**).

    The plan is picked once, as reward against risk in gems (``adk3.realm.harm`` plays each option out
    with the Manual's dice), with close calls rolled, leaning bolder as the ``risk`` lever rises:

    - **fight** to the end, unless health reaches the bar: then drink if we carry a potion and fight on,
      else break off to safe ground and say so;
    - **back off** to safe ground, then **hold** there;
    - **go on** with the method, until we are hit or a new hostile turns up.

    Holding means the approach cannot go on past these hostiles: it is reported as held up, which fails
    the approach (``lou.publish``), so plan B or the strategist moves her elsewhere. A hold lasts until
    the approach changes or the hostiles are gone. It is planned again only when the engagement ends
    or the bar is reached, never tick by tick.
    """

    name = "combat"

    def __init__(self):
        self.now: Engagement | None = None
        self.threat_ids: set[int] = set()

    def applies(self, ctx: Ctx, order: Order | None) -> bool:
        if isinstance(order, Fight) and order.boss:
            self.now = None
            return False
        s = ctx.state
        threats = harm.threats_near(s)
        if not threats:
            if self.now is not None and self.now.kind == "back_off":
                self.report(ctx, f"backed off from {self.now.why}; clear now")
            self.now = None
            return False
        if self.now is None or self.over(ctx, threats):
            self.now = self.plan(ctx, order, threats)
        if self.now.kind == "back_off" and s.on_safe_ground:
            self.hold(ctx, self.now.why)
        self.threat_ids = {e.id for e in threats}
        return self.now.kind not in ("go_on",)

    def act(self, ctx: Ctx, order: Order | None) -> Action | None:
        s, e = ctx.state, self.now
        if e.kind == "fight":
            target = next((x for x in s.npcs() if x.id == e.target.id), None)
            if target is None:
                return None
            if s.health <= e.bar:
                drink = best_potion(ctx)
                if drink is not None:
                    return drink
                self.report(ctx, f"broke off a fight with {target.code} at {s.health} health (the bar is {e.bar:g}), no potion")
                self.now = self.make(ctx, "back_off", None, f"a {target.code} we could not finish")
                return self.act(ctx, order)
            return self.swing_at(ctx, target)
        if e.kind == "back_off":
            away = travel.to_safe(ctx)
            if away:
                return walk_or_break(ctx, away, "to safe ground")
            run = flee_run(s.world, harm.threats_near(s), set(), _first_away(ctx))
            return actions.Walk(tuple(run), "away") if run else actions.Stop("cornered")
        if e.kind == "drink":
            self.now = None
            return best_potion(ctx)
        return actions.Stop(f"holding on safe ground: {e.why} in the way")

    def over(self, ctx: Ctx, threats: list[Entity]) -> bool:
        """The engagement has run its course: its target is dead or gone, we are clear, or a go-on was hit or met a new hostile."""
        s, e = ctx.state, self.now
        if e.kind == "fight":
            gone = all(x.id != e.target.id for x in threats)
            if gone:
                self.report(ctx, f"the fight with {e.target.code} is over")
            return gone
        if e.kind == "back_off":
            return False  # until safe ground (then held) or clear (``applies``)
        if e.kind == "hold":
            return e.approach is not ctx.approach
        if e.kind == "go_on":
            return s.health < e.hits_at_start or any(x.id not in self.threat_ids for x in threats)
        return True

    def plan(self, ctx: Ctx, order: Order | None, threats: list[Entity]) -> Engagement:
        s = ctx.state
        seed = s.tick
        price = Price(ctx)
        ahead = course_of(ctx, order)
        per_step = step_worth(ctx)
        scored = []
        for o in self.options(ctx, ahead, threats):
            odds = harm.odds(s, o.course, seed=seed, territory=ctx.memory.territory)
            scored.append((o.reward + odds.steps * per_step + odds.kill * o.kill_worth - price.risk(odds), o))
        pick, odds_text = roll(scored, ctx.levers.risk, random.Random(seed))
        why = pick.target.code if pick.target is not None else ", ".join(sorted({e.code for e in threats}))
        ctx.memory.note(f"engaged: {pick.name} ({odds_text})", news=False)
        if pick.kind == "hold":
            return self.hold(ctx, why)
        return self.make(ctx, pick.kind, pick.target, why)

    def make(self, ctx: Ctx, kind: str, target: Entity | None, why: str) -> Engagement:
        s = ctx.state
        return Engagement(kind, target, bar(ctx, target), s.tick, s.tick, s.health, why, ctx.approach)

    def hold(self, ctx: Ctx, why: str) -> Engagement:
        """Stay on safe ground; the approach is held up by ``why``."""
        s = ctx.state
        ctx.memory.held_up_by(f"{why} near {s.pos[0]},{s.pos[1]}; holding on safe ground")
        self.now = self.make(ctx, "hold", None, why)
        return self.now

    def options(self, ctx: Ctx, ahead: tuple[Pos, ...], threats: list[Entity]) -> list[Option]:
        s = ctx.state
        out = [Option("go on", "go_on", harm.Course(ahead))]
        avoid = set(ctx.tactics.get("avoid") or [])
        for e in threats:
            if e.code in avoid:
                continue
            reach = s.world.attack_range or 1
            approach = tuple(travel.to_beside(ctx, e.pos, reach) or ()) if chebyshev(e.pos, s.pos) > reach else ()
            worth = ctx.memory.gems_per_kill(e.code, KILL_DROP) * ctx.levers.greed_weight
            out.append(Option(f"fight {e.code}", "fight", harm.Course(approach, target=e.id, then=ahead),
                              kill_worth=worth, target=e))
        if s.on_safe_ground:
            out.append(Option("hold", "hold", harm.Course()))
        else:
            away = travel.to_safe(ctx)
            path = tuple(away) if away else tuple(flee_run(s.world, threats, set(), _first_away(ctx)) or ())
            out.append(Option("back off", "back_off", harm.Course(path)))
        drink = best_potion(ctx)
        if drink is not None:
            out.append(Option(str(drink), "drink", harm.Course(heal=supplies.heal_of(drink.code), path=ahead),
                              reward=-value.item(drink.code)))
        return out

    def swing_at(self, ctx: Ctx, e: Entity) -> Action | None:
        s = ctx.state
        reach = s.world.attack_range or 1
        if chebyshev(e.pos, s.pos) > reach:
            return walk_or_break(ctx, travel.to_beside(ctx, e.pos, reach) or (), f"close on {e.code}")
        if s.on_safe_ground:
            return actions.Stop("no swing lands from safe ground")  # Manual §11 Combat
        return actions.Swing(e.id, f"{e.code} {e.id}") if s.attack_ready else actions.Stop("between swings")

    def report(self, ctx: Ctx, text: str) -> None:
        ctx.memory.note(f"{text} near {ctx.state.pos[0]},{ctx.state.pos[1]}")


def roll(scored: list[tuple[float, Option]], risk: float, rng: random.Random) -> tuple[Option, str]:
    """The best option, or among those within ``CLOSE_CALL`` of it a roll weighted toward the better, and toward
    fighting as the ``risk`` lever rises above 5. Returns the pick and its odds, for the log."""
    best = max(s for s, _ in scored)
    close = [(s, o) for s, o in scored if s >= best - CLOSE_CALL]
    lean = (risk - 5) / 5  # -1 cautious .. +1 bold
    weights = [math.exp((s - best) / (CLOSE_CALL / 2)) * (1 + 0.5 * lean if o.kind == "fight" else 1) for s, o in close]
    total = sum(weights)
    r, acc = rng.random() * total, 0.0
    for w, (_, o) in zip(weights, close):
        acc += w
        if r <= acc:
            pick = o
            break
    else:
        pick = close[-1][1]
    odds = ", ".join(f"{o.name} {w / total:.0%}" for w, (_, o) in zip(weights, close))
    return pick, odds


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


def _first_away(ctx: Ctx) -> Pos:
    s = ctx.state
    threats = harm.threats_near(s)
    options = s.world.open_neighbours(s.pos)
    if not options or not threats:
        return s.pos
    return max(options, key=lambda p: min(chebyshev(p, e.pos) for e in threats))


# The boss room.

class Boss:
    """The boss fight on its clock (Manual §11 Bosses and levels): fight to the bar, drink, and leave by a door
    before the limit or when losing."""

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
        if s.health <= bar(ctx, boss):
            drink = best_potion(ctx)
            if drink is not None:
                return drink
        if boss is None:
            return None
        return Combat().swing_at(ctx, boss)


# Opportunities.

class Opportunities:
    """Nothing threatens and something in view is worth more than its detour: take it."""

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
        reach = ctx.tactics.get("detour")  # the approach's limit on how far to stray, in blocks
        per_step = step_worth(ctx)
        best, best_net = None, 0.0
        for c in near_first(ctx, chances(ctx)):
            if reach is not None and chebyshev(s.pos, c.at) - c.reach > float(reach):
                continue
            if chebyshev(s.pos, c.at) <= c.reach:
                net, action = c.worth, c.action
            else:
                path = travel.to_beside(ctx, c.at, c.reach)
                if path is None:
                    continue
                net, action = c.worth - len(path) * per_step, walk_or_break(ctx, path, c.why)
            if net > best_net and action is not None:
                best, best_net = action, net
        self._pick = best
        return best


# The method's own reactions.

class Travel:
    """Walk where the method goes, replanned every decision; break what is in the way when we can."""

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
    """Stand within reach and do the one thing the method asks: take, read, talk, use, equip, empty a chest."""

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
            return actions.UseOn(cell[0], cell[1], arm_for(ctx, "bush"), order.why)  # grass falls to what cuts bushes
        return walk_or_break(ctx, work_path(ctx, order) or (), order.why)


ALL = [Combat(), Boss(), Opportunities(), Travel(), Interact(), WorkBlocks()]
