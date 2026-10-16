"""Lorraine's reflexes (A141): what runs above the op, every round, before the tree is walked.

In order, the first that fires holds the round:

- **sync**: her place is unknown. Asleep, a ``Wait`` wakes her; else she
  holds until a position read places her.
- **downed**: dead. She holds until the world respawns her.
- **flee**: a hostile is in range and her margin cannot pay for the
  hostiles that can reach her: she steps away (``pathing.flee_step``, then
  ``flee_run``), toward the nearest safe cell.
- **retreat**: the margin is short and hostiles can reach her, none in
  range yet: she walks to safe ground (``survival.safe_goals``).
- **heal**: in a fight, health alone no longer pays for it but the heals
  carried do: she drinks or eats one.
- **fight back**: a hostile hits her from her weapon's reach and the margin
  pays for the fight: she swings, rather than walking on taking hits.

Every one of them reads one number, her margin now
(``readiness.margin_now`` on :class:`readiness.Here`, the hostiles that can
reach her valued as the goal she works): her one risk function, never a
threshold of its own (A139). A reflex never sets a goal: it holds the round,
and the next walk of the tree decides what she works on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from agentrealm_adk.executor import arm_then_use, attack_cooldown_ticks, build_attack_queue, use_cooldown_ticks, wait
from agentrealm_adk.executor.movement import build_paced_walk_queue
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.pathing import flee_run, flee_step, plan_sets
from agentrealm_adk.reach import weapon_reach
from agentrealm_adk.supplies import heal_of, heals
from agentrealm_adk.survival import hostiles_in_range, is_attacker, is_hostile, on_safe_tile, recently_attacked, safe_goals
from agentrealm_adk.world import Entity, chebyshev

from .act import Ctx, arm, use_on, use_self, walk
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


def reflex(ctx: Ctx) -> Reflex | None:
    """The first reflex that fires this round, else None: the op runs."""
    for check in REFLEXES:
        fired = check(ctx)
        if fired is not None:
            return fired
    return None


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
    first = flee_step(w, hostiles, blocked, safe_goals(w, ctx.kb))
    if first is None:
        drink = heal_now(ctx)
        if drink is not None:
            return Reflex("flee", drink, f"cornered: drink ({b.why()})")
        swing = swing_at_hitter(ctx)
        return Reflex("flee", swing, f"cornered: {'swing' if swing else 'hold'} ({b.why()})")
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
    goals = safe_goals(w, ctx.kb)
    if not goals:
        return None
    a = walk(ctx, RETREAT_GOAL, goals[0])
    return Reflex("retreat", a.intents, f"retreat to {goals[0]}: {b.why()}")


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
    carried = [s for s in w.held_supplies if heals(s.code)]
    if w.armed_id is not None and heals(w.armed_code) and all(s.id != w.armed_id for s in carried):
        carried.append(InventorySupply(w.armed_id, w.armed_code or ""))
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
    """Swings at the hostile hitting her, when it stands in her weapon's reach."""
    w, m = ctx.world, ctx.mem
    reach = weapon_reach(w, ctx.kb)
    hitter = next(
        (e for e in w.entities if is_attacker(w, e) and is_hostile(w, ctx.policy, e) and chebyshev(e.pos, w.pos) <= reach),
        None,
    )
    if hitter is None:
        return None
    since = max(1, w.tick - m.last_use_tick) if m.last_use_tick is not None else None
    queue = build_attack_queue(
        [use_on(hitter)],
        poll_interval_ticks=ctx.horizon,
        horizon_ticks=ctx.horizon,
        weapon_cooldown_ticks=attack_cooldown_ticks(w.armed_code, ctx.tick_hz),
        ticks_since_last_use=since,
    )
    return queue or None


# The order they are asked in: the first that fires holds the round.
REFLEXES = (sync, downed, flee, retreat, heal, fight_back)
