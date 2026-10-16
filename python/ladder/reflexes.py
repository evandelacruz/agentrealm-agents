"""Lorraine's reflexes (A141): what runs above the op, every round, before the tree is walked.

In order, the first that fires with something to send holds the round:

- **sync**: her place is unknown. Asleep, a ``Wait`` wakes her; else she
  holds until a position read places her.
- **downed**: dead. She holds until the world respawns her.
- **flee**: a hostile is in range and her margin cannot pay for the
  hostiles that can reach her: she steps away (``pathing.flee_step``, then
  ``flee_run``), toward the refuge. Cornered, she drinks, else swings.
- **retreat**: the margin is short and hostiles can reach her, none in
  range yet: she walks to the refuge.
- **heal**: in a fight, health alone no longer pays for it but the heals
  carried do: she drinks or eats one.
- **fight back**: a hostile hits her from her weapon's reach and the margin
  pays for the fight: she swings, rather than walking on taking hits.

Only sync and downed hold the round with nothing to send. Any other reflex
with nothing to send, or whose intents the water gate cuts to nothing
(``fording.ford_gate``, A133, run by the loop), yields to the next one and
then to the op: she never stands taking hits for want of a move (A114). Flee
and retreat head for one refuge, ``pathing.retreat_safe_goal``: the nearest
safe block she gets to before any hostile, each priced as a chaser (A162).

Every one of them reads one number, her margin now
(``readiness.margin_now`` on :class:`readiness.Here`, the hostiles that can
reach her valued as the goal she works): her one risk function, never a
threshold of its own (A139). A reflex never sets a goal: it holds the round,
and the next walk of the tree decides what she works on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from agentrealm_adk.executor import arm_then_use, use_cooldown_ticks, wait
from agentrealm_adk.executor.movement import build_paced_walk_queue
from agentrealm_adk.executor.supply_refusals import in_hand
from agentrealm_adk.pathing import flee_run, flee_step, plan_sets, retreat_safe_goal
from agentrealm_adk.reach import weapon_reach
from agentrealm_adk.supplies import heal_of, heals
from agentrealm_adk.survival import hostiles_in_range, is_attacker, is_hostile, on_safe_tile, recently_attacked
from agentrealm_adk.world import Entity, Pos, chebyshev

from .act import Ctx, arm, swings, use_self, walk
from .readiness import Budget, Here, margin_now, room

# The goal a retreat walk is given up under, as any walk is (A90).
RETREAT_GOAL = "lorraine:retreat"


@dataclass(frozen=True)
class Reflex:
    """A reflex that fired: its name, the intents it sends (None holds the round), and why."""

    name: str
    intents: list[dict] | None
    why: str


def margin(ctx: Ctx) -> Budget:
    """Her margin now against the hostiles that can reach her, valued as the goal she works."""
    return margin_now(Here(ctx.value), ctx.world, ctx.kb)


def reflex(ctx: Ctx, gate: Callable[[str, list[dict]], list[dict]] | None = None) -> Reflex | None:
    """The first reflex that fires this round with something to send, else
    None: the op runs. ``gate`` is the water gate the loop sends through;
    a reflex it cuts to nothing yields. Only sync and downed hold with nothing."""
    for check in REFLEXES:
        fired = check(ctx)
        if fired is None:
            continue
        if fired.name in HOLDS:
            return fired
        intents = gate(fired.name, fired.intents) if gate is not None and fired.intents else fired.intents
        if intents:
            return replace(fired, intents=intents)
    return None


def refuge(ctx: Ctx) -> Pos | None:
    """Where flee runs toward and retreat walks (``pathing.retreat_safe_goal``, A162)."""
    _, avoid, costly = plan_sets(ctx.world, ctx.mem, ctx.policy, ctx.kb)
    return retreat_safe_goal(ctx.mem, ctx.world, ctx.policy, ctx.kb, avoid, costly)


def sync(ctx: Ctx) -> Reflex | None:
    w = ctx.world
    if w.pos is not None or not w.alive:
        return None
    if w.asleep:
        ctx.mem.need_self = True  # read self again, so a wake the round trip did not report never loops
        return Reflex("sync", [wait()], "wake")
    return Reflex("sync", None, "waiting for a position")


def downed(ctx: Ctx) -> Reflex | None:
    return None if ctx.world.alive else Reflex("downed", None, "downed: waiting to respawn")


def threats(ctx: Ctx) -> list[Entity]:
    """The hostiles in range, and the one hitting her when it is in view."""
    w = ctx.world
    near = hostiles_in_range(w, ctx.policy)
    hitter = [e for e in w.entities if is_attacker(w, e) and is_hostile(w, ctx.policy, e) and e not in near]
    return near + (hitter if recently_attacked(w) else [])


def flee(ctx: Ctx) -> Reflex | None:
    w = ctx.world
    if on_safe_tile(w):
        return None
    hostiles = threats(ctx)
    if not hostiles:
        return None
    b = margin(ctx)
    if b.ok:
        return None
    blocked, _, _ = plan_sets(w, ctx.mem, ctx.policy, ctx.kb)
    safe = refuge(ctx)
    first = flee_step(w, hostiles, blocked, [safe] if safe is not None else ())
    if first is None:
        # Cornered: a drink, else a swing; with neither the next reflex acts.
        drink = heal_now(ctx)
        if drink is not None:
            return Reflex("flee", drink, f"cornered: drink ({b.why()})")
        swing = swing_at_hitter(ctx)
        return Reflex("flee", swing, f"cornered: swing ({b.why()})") if swing else None
    run = flee_run(w, hostiles, blocked, first)
    m = ctx.mem
    since = max(1, w.tick - m.last_step_tick) if m.last_step_tick is not None else None
    queue = build_paced_walk_queue(
        w.pos, run, movement_speed_milli=w.movement_speed, tick_rate_hz=ctx.tick_hz, ticks_since_last_step=since
    )
    nearest = min(hostiles, key=lambda e: (chebyshev(e.pos, w.pos), e.id))
    return Reflex("flee", queue[: ctx.horizon], f"flee {nearest.kind} {nearest.id}: {b.why()}")


def retreat(ctx: Ctx) -> Reflex | None:
    w = ctx.world
    if on_safe_tile(w) or not Here().foes(w, ctx.kb):
        return None
    b = margin(ctx)
    if b.ok:
        return None
    goal = refuge(ctx)
    if goal is None:
        return None
    a = walk(ctx, RETREAT_GOAL, goal)
    return Reflex("retreat", a.intents, f"retreat to {goal}: {b.why()}") if a.intents else None


def heal(ctx: Ctx) -> Reflex | None:
    """In a fight, a heal once health alone no longer pays for it."""
    w = ctx.world
    if not (threats(ctx) or recently_attacked(w)):
        return None
    b = margin(ctx)
    if not b.ok or b.heal == 0 or replace(b, heal=0).ok:
        return None  # short even with the heals (flee's), or health alone pays (nothing to drink for)
    drink = heal_now(ctx)
    return Reflex("heal", drink, f"health alone short: {b.why()}") if drink else None


def heal_now(ctx: Ctx) -> list[dict] | None:
    """Drink or eat the carried heal that wastes least, armed first when it is not in hand."""
    w = ctx.world
    space = room(w, w.health or 0)
    carried = [s for s in in_hand(w) if heals(s.code)]  # held and armed: each supply in one place (A154)
    if not carried or space <= 0:
        return None
    pick = min(carried, key=lambda s: (abs(space - heal_of(s.code)), s.id))
    use = use_self()
    if pick.id == w.armed_id:
        return [use]
    queue = arm_then_use(
        arm(pick.id), use, cooldown_ticks=use_cooldown_ticks(use, pick.code, ctx.tick_hz), horizon_ticks=ctx.horizon
    )
    return queue or None


def fight_back(ctx: Ctx) -> Reflex | None:
    w = ctx.world
    if not recently_attacked(w):
        return None
    b = margin(ctx)
    if not b.ok:
        return None
    swing = swing_at_hitter(ctx)
    return Reflex("fight back", swing, f"hit back: {b.why()}") if swing else None


def swing_at_hitter(ctx: Ctx) -> list[dict] | None:
    """A swing at the hostile hitting her, when it stands in her weapon's
    reach, the weapon to fight with armed first (``act.swings``)."""
    w = ctx.world
    reach = weapon_reach(w, ctx.kb)
    hitter = next(
        (e for e in w.entities if is_attacker(w, e) and is_hostile(w, ctx.policy, e) and chebyshev(e.pos, w.pos) <= reach),
        None,
    )
    if hitter is None:
        return None
    return swings(ctx, hitter) or None


# The order they are asked in: the first that fires with something to send holds the round.
REFLEXES = (sync, downed, flee, retreat, heal, fight_back)
# The reflexes that hold the round with nothing to send: there is nothing else to do.
HOLDS = ("sync", "downed")
# The reflexes that run away: the water gate arms no weapon in front of them (``fording.ESCAPES``).
ESCAPES = ("flee", "retreat")
