"""What Lorraine's goal tree reads (A139): one risk function and the facts of the next level.

Every "am I prepared" in Lorraine is computed here, once. The goal tree
(``ladder/goals.py``) reads it for its readiness predicates, and the reflexes
(A141) will read the same :func:`risk_budget` instead of thresholds of their
own (:func:`margin_now` is their hook).

**One risk function.** :func:`risk_budget` compares what the character has
to spend, health plus the heals carried (``healing.held_heal``, A132), with
the expected cost of the goal being worked: the damage its hostiles are
expected to deal while she kills them one by one. That cost is Gary's win
estimate, reused, not copied: the threat table and the published rolls
(``survival.expected_damage`` over ``survival.ticks_to_kill_them``), armor
counted as worn (``survival.worn_defense``). A goal may cost a share of what
she has, by its value (``VALUE_RISK_SHARE``): an ordinary goal half of it,
a valuable one (a boss, a gem cache) three quarters. "Ready for its risk" is
this function at level scale; "stay alive" is the same function on the goal
being worked, counting only the heals usable now (``now``).

**What is missing.** :func:`missing` lists what the next level still needs
bought, from either branch of the tree: the item its entrance needs (a look's
or a clue's, ``entrance_needs``, A147) and the gear and heals that would close
its risk, each priced from a known seller (``sellers.sellers_of``, A131).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from agentrealm_agent.discovery import seen_prices
from agentrealm_agent.entrance_needs import lying_in_sight, meets, unmet_need
from agentrealm_agent.equip import hunting_ceiling, weapon_rank
from agentrealm_agent.healing import held_heal
from agentrealm_agent.knowledge_maps import map_level_known
from agentrealm_agent.level_order import CLEARED, entrance_rank, target_level
from agentrealm_agent.sellers import sellers_of
from agentrealm_agent.supplies import armor_defense, heal_of, heals, is_weapon, sold_codes, worn_slot
from agentrealm_agent.survival import (
    FIGHT_REACH_RADIUS,
    UNKILLED_HOSTILE_HEALTH,
    estimate_health,
    expected_damage,
    known_hostile,
    ticks_to_kill_them,
    worn_defense,
)
from agentrealm_agent.travel.knowledge import iter_entrances, iter_shop_sales
from agentrealm_agent.world import Entity, Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from agentrealm_agent.knowledge_base import KnowledgeBase

# The value-to-risk rule: the share of health plus held heals a goal may be
# expected to cost, by what the goal is worth. A valuable goal (a level, which
# ends in its boss; a gem cache) may cost more than an ordinary one, and no
# goal may cost all of it.
ORDINARY, VALUABLE = "ordinary", "valuable"
VALUE_RISK_SHARE = {ORDINARY: 0.5, VALUABLE: 0.75}

# Assumed until measured: a level nothing has been seen in holds this many
# hostiles of a type never measured (the world's base attack power, A85).
# Hostiles seen on the level's maps replace them.
LEVEL_PRIOR_FOES = 3
# A stand-in hostile for those assumptions: no type, so priced unmeasured.
PRIOR_FOE = Entity("npc", 0, (0, 0))

# The most purchases :func:`missing` plans to close a level's risk.
MOST_RISK_BUYS = 6
# Why a purchase is on the list, as :func:`missing` files it.
NEED_WHY = "the entrance needs {}"
RISK_WHY = "the level's risk"


class RiskyGoal(Protocol):
    """A goal :func:`risk_budget` can price: its hostiles and its value."""

    value: str

    def foes(self, world: WorldModel, kb: KnowledgeBase | None) -> list[Entity]: ...


@dataclass(frozen=True)
class Loadout:
    """What a fight is priced with: the weapon swung, the defense worn, the heals carried."""

    weapon: str | None
    armor: int
    heal: int
    worn: dict[str, int] = field(default_factory=dict)  # slot -> defense worn there

    @classmethod
    def now(cls, w: WorldModel) -> Loadout:
        """As armed and worn now, as Gary's survival module counts them."""
        worn = {slot: armor_defense(code) or 0 for slot, code in w.worn_codes.items()}
        return cls(w.armed_code, worn_defense(w), held_heal(w), worn)


@dataclass(frozen=True)
class Budget:
    """A goal's expected cost against what the character has to spend on it."""

    health: int
    heal: int
    cost: float
    value: str

    @property
    def have(self) -> int:
        return self.health + self.heal

    @property
    def allowed(self) -> float:
        return VALUE_RISK_SHARE[self.value] * self.have

    @property
    def ok(self) -> bool:
        return self.have > 0 and self.cost <= self.allowed

    @property
    def slack(self) -> float:
        return self.allowed - self.cost

    @property
    def short(self) -> int:
        """Health plus heals still lacking for the cost to fit the goal's share."""
        return max(0, math.ceil(self.cost / VALUE_RISK_SHARE[self.value]) - self.have)

    def why(self) -> str:
        sign = "<=" if self.ok else ">"
        return (
            f"cost {self.cost:.1f} {sign} {self.allowed:.1f} allowed "
            f"(health {self.health} + heals {self.heal}, {self.value})"
        )


def fight_cost(foe: Entity, w: WorldModel, loadout: Loadout) -> float:
    """The damage ``foe`` is expected to deal while ``loadout`` kills it alone:
    Gary's win estimate, its health scaled from his assumed hostile's when a
    read served it (a boss)."""
    hp = foe.max_health or foe.health or UNKILLED_HOSTILE_HEALTH
    ticks = ticks_to_kill_them([foe], loadout.weapon) * hp / UNKILLED_HOSTILE_HEALTH
    return expected_damage([foe], w.threat, ticks, loadout.armor)


def risk_budget(
    goal: RiskyGoal,
    world: WorldModel,
    kb: KnowledgeBase | None,
    loadout: Loadout | None = None,
    *,
    now: bool = False,
) -> Budget:
    """The one risk function: health plus held heals against the expected
    cost of ``goal``'s hostiles, fought one by one with ``loadout`` (as
    armed and worn now by default), within the share its value allows.

    ``now`` counts only the heals that can be used now: up to max health, so
    a heal past it, wasted if used, counts for nothing. A goal being worked
    ("stay alive", the reflexes) is priced so; a level ahead, where heals are
    spent across many fights, counts them all."""
    lo = loadout or Loadout.now(world)
    cost = sum(fight_cost(f, world, lo) for f in goal.foes(world, kb))
    health = estimate_health(world)
    heal = min(lo.heal, room(world, health)) if now else lo.heal
    return Budget(health, heal, cost, goal.value)


def room(w: WorldModel, health: int) -> int:
    """Health a heal can still add: max health less ``health``."""
    return max(0, (w.max_health or health) - health)


def margin_after(goal: RiskyGoal, world: WorldModel, kb: KnowledgeBase | None, *, eat: int = 0, carry: int = 0) -> Budget:
    """The margin now (``risk_budget`` with ``now``) once ``eat`` health is
    eaten (food in sight) or ``carry`` more heal is held (a potion bought).
    A heal carried and then drunk leaves it as it was: it counted already."""
    now = risk_budget(goal, world, kb, now=True)
    health = min(now.health + eat, now.health + room(world, now.health))
    heal = min(held_heal(world) + carry, room(world, health))
    return Budget(health, heal, now.cost, now.value)


def margin_now(goal: RiskyGoal, world: WorldModel, kb: KnowledgeBase | None) -> Budget:
    """The hook the reflexes (A141) read: the goal being worked, priced now."""
    return risk_budget(goal, world, kb, now=True)


# The next level and what it holds.


def map_level(w: WorldModel, kb: KnowledgeBase | None, map_id: int) -> int | None:
    """``map_id``'s level from a minimap read, this run's or a saved one (A145)."""
    level = w.map_levels.get(map_id)
    return level if level is not None else map_level_known(kb, map_id)


def next_entrance(w: WorldModel, kb: KnowledgeBase | None) -> tuple[int, Pos] | None:
    """The entrance to try for the next level, by A145's order: one known to
    be the target, else the nearest one not yet entered; never a barred or a
    cleared one."""
    here = w.pos or (0, 0)
    ranked = [
        (rank, mid != w.map_id, chebyshev(here, pos), mid, pos)
        for mid, pos, row in iter_entrances(kb)
        if (rank := entrance_rank(row, w)) is not None and rank != CLEARED
    ]
    if not ranked:
        return None
    _, _, _, mid, pos = min(ranked)
    return mid, pos


def level_foes(w: WorldModel, kb: KnowledgeBase | None) -> list[Entity]:
    """The hostiles the next level is expected to hold: every known hostile
    remembered on its maps (``survival.known_hostile``: ``sightings`` keeps
    every NPC and character seen, helpers and other players too), else
    ``LEVEL_PRIOR_FOES`` unmeasured ones."""
    level = target_level(w)
    seen = [
        s.entity
        for s in w.sightings.values()
        if level is not None
        and s.map_id is not None
        and map_level(w, kb, s.map_id) == level
        and known_hostile(w, s.entity)
    ]
    return seen or [PRIOR_FOE] * LEVEL_PRIOR_FOES


def foes_here(w: WorldModel, kb: KnowledgeBase | None) -> list[Entity]:
    """The hostiles that can reach the character where she works: known
    hostiles (``survival.known_hostile``) in view or remembered on this map
    within the fight's reach (``survival.FIGHT_REACH_RADIUS``)."""
    if w.pos is None:
        return []
    near: dict[tuple[str, int], Entity] = {}
    for e in w.entities:
        if known_hostile(w, e) and chebyshev(w.pos, e.pos) <= FIGHT_REACH_RADIUS:
            near[(e.kind, e.id)] = e
    for key, s in w.sightings.items():
        if s.map_id == w.map_id and known_hostile(w, s.entity) and chebyshev(w.pos, s.entity.pos) <= FIGHT_REACH_RADIUS:
            near.setdefault(key, s.entity)
    return list(near.values())


# What is missing, and its price.


@dataclass(frozen=True)
class Purchase:
    code: str
    price: int
    why: str


@dataclass
class Missing:
    """What the next level still needs bought, priced, and what has no known seller."""

    buys: list[Purchase] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)

    @property
    def gems(self) -> int:
        return sum(p.price for p in self.buys)


def price(code: str, w: WorldModel, kb: KnowledgeBase | None) -> int | None:
    """``code``'s price from a seller known to sell it (A131), else None. A
    shop cell counts only for the very item it was seen selling: the seller
    test takes any potion for a potion (A21), but what a potion heals is
    priced by the one on the shelf."""
    shelves = {(mid, pos) for mid, pos, sold, _ in iter_shop_sales(kb) if sold == code}
    sellers = [s for s in sellers_of(code, w, kb) if s.by != "shop" or (s.map_id, s.pos) in shelves]
    if not sellers:
        return None
    quoted = [s.price for s in sellers if s.price]
    return min(quoted) if quoted else seen_prices(w, kb).get(code)


def for_sale(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, int]:
    """Every item a known seller sells, with its price."""
    out = {}
    for code in sorted({*sold_codes(), *seen_prices(w, kb)}):
        p = price(code, w, kb)
        if p is not None:
            out[code] = p
    return out


def owned(w: WorldModel) -> list[str]:
    codes = [s.code for s in w.held_supplies + w.chest_supplies] + [w.armed_code or ""] + list(w.worn_codes.values())
    return [c for c in codes if c]


def best_owned(w: WorldModel, kb: KnowledgeBase | None) -> Loadout:
    """The loadout once the best gear she owns is armed and worn (Equip's rule, A99)."""
    lo = Loadout.now(w)
    for code in owned(w):
        lo = _with(lo, code, w, kb, heal=False)
    return lo


def _with(lo: Loadout, code: str, w: WorldModel, kb: KnowledgeBase | None, *, heal: bool = True) -> Loadout:
    """``lo`` with ``code`` added: a heal carried, a better weapon armed, better armor worn."""
    if heal and heals(code):
        return replace(lo, heal=lo.heal + heal_of(code))
    if is_weapon(code):
        ceiling = hunting_ceiling(w, kb)
        if weapon_rank(code, w, ceiling) > weapon_rank(lo.weapon if is_weapon(lo.weapon) else None, w, ceiling):
            return replace(lo, weapon=code)
        return lo
    defense, slot = armor_defense(code) or 0, worn_slot(code)
    if slot and defense > lo.worn.get(slot, 0):
        return replace(lo, armor=lo.armor - lo.worn.get(slot, 0) + defense, worn={**lo.worn, slot: defense})
    return lo


def need_of_next_level(w: WorldModel, kb: KnowledgeBase | None) -> str | None:
    """The item or capability the next level's entrance needs that nothing carried meets (A147)."""
    entrance = next_entrance(w, kb)
    return unmet_need(w, kb, *entrance) if entrance else None


def missing(w: WorldModel, kb: KnowledgeBase | None, level: RiskyGoal) -> Missing:
    """What the next level still needs bought: its entrance's need (unless
    it lies in sight for the taking), then the cheapest gear and heals, by
    slack gained per gem, that bring ``level``'s risk within its share."""
    out = Missing()
    sale = for_sale(w, kb)
    need = need_of_next_level(w, kb)
    if need and lying_in_sight(w, need) is None:
        offers = sorted((p, c) for c, p in sale.items() if meets(c, need))
        if offers:
            out.buys.append(Purchase(offers[0][1], offers[0][0], NEED_WHY.format(need)))
        else:
            out.unpriced.append(need)
    lo = best_owned(w, kb)
    budget = risk_budget(level, w, kb, lo)
    for _ in range(MOST_RISK_BUYS):
        if budget.ok:
            break
        best = None
        for code, p in sale.items():
            gain = risk_budget(level, w, kb, _with(lo, code, w, kb)).slack - budget.slack
            if gain > 0 and (best is None or gain / p > best[0]):
                best = (gain / p, code, p)
        if best is None:
            out.unpriced.append(f"{budget.short} health or heals")
            break
        _, code, p = best
        out.buys.append(Purchase(code, p, RISK_WHY))
        lo = _with(lo, code, w, kb)
        budget = risk_budget(level, w, kb, lo)
    return out


def food_in_sight(w: WorldModel) -> Entity | None:
    """The nearest free heal lying in sight."""
    here = w.pos
    free = [e for e in w.entities if e.kind == "supply" and e.gem_price is None and heals(e.code)]
    if here is None or not free:
        return None
    return min(free, key=lambda e: (chebyshev(here, e.pos), e.pos))


def foes_line(foes: list[Entity]) -> str:
    return ", ".join(sorted(e.code or "unmeasured" for e in foes))

