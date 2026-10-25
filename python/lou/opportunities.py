"""Opportunities: the reactive rules, things worth stopping for (docs/v3/SPEC.md **Engines**).

Each kind finds its chances in what is known and prices each in gems. The
opportunities engine weighs each against its detour (the steps, at what
time is worth, and the danger on the way) and takes the best one worth
more than it costs. A new reactive rule is a new class in ``ALL``.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_adk import supplies
from agentrealm_adk.oddities import is_odd
from agentrealm_adk.world import Pos, chebyshev

from adk3.realm import actions, travel, value
from adk3.realm.actions import Action
from adk3.realm.context import Ctx
from adk3.realm.memory import place_key

SAY_REACH = 20  # blocks: a Say to an NPC reaches 25 (Manual §11 Speech); a little inside it
READ_REACH = 5  # blocks: near enough that the sign is in sight in any light
CLUE_WORTH = 15  # gems a new clue is worth at curiosity 5: about two and a half minutes of field work
ODD_BLOCK_WORTH = 5  # gems: the smallest gem cache an oddity drops (Manual §16)
LOOT_WORTH = 2  # gems an unpriced item lying free is worth at greed 5, when it is not sold anywhere
CHEST_LOOK_WORTH = 5  # gems a look into an unknown ground chest is worth at curiosity 5: its contents show only in reach


@dataclass(frozen=True)
class Chance:
    action: Action
    at: Pos  # where it is done from: we stand within ``reach`` of it
    reach: int
    worth: float  # gems
    why: str


class Gems:
    """Free gems on the ground: their count (Manual §11 Supplies)."""

    def find(self, ctx: Ctx) -> list[Chance]:
        w = ctx.levers.greed_weight
        return [Chance(actions.Take(e.id, e.code), e.pos, 1, w * value.gems_of(e.code), f"pick up {e.code}")
                for e in ctx.state.free_gems()]


class Hearts:
    """A life on the ground: what a life is worth to us."""

    def find(self, ctx: Ctx) -> list[Chance]:
        return [Chance(actions.Take(e.id, e.code), e.pos, 1, ctx.levers.life_worth, "pick up a life")
                for e in ctx.state.supplies() if not e.gem_price and supplies.is_life(e.code)]


class Loot:
    """Any other item lying free (a weapon, a tool, a chest upgrade): its list price, by greed. Gems, lives and
    food have their own rules; a priced supply is a shop shelf; what we dropped ourselves is not picked back up."""

    def find(self, ctx: Ctx) -> list[Chance]:
        s, mem = ctx.state, ctx.memory
        out = []
        for e in s.supplies():
            if (e.gem_price or s.world.is_our_drop(e.id) or value.gems_of(e.code) or supplies.is_life(e.code)
                    or supplies.eaten_on_pickup(e.code) or place_key(s.map_id, e.pos) in mem.no_effect):
                continue
            worth = (value.item(e.code) or LOOT_WORTH) * ctx.levers.greed_weight
            out.append(Chance(actions.Take(e.id, e.code), e.pos, 1, worth, f"pick up {e.code}"))
        return out


class Food:
    """Food when hurt: the health it gives back (Manual §11 Food and potions: eaten on pickup, even at full health)."""

    def find(self, ctx: Ctx) -> list[Chance]:
        s = ctx.state
        missing = s.max_health - s.health
        out = []
        for e in s.supplies():
            heal = supplies.heal_of(e.code)
            if e.gem_price or not supplies.eaten_on_pickup(e.code) or heal <= 0 or missing < heal:
                continue
            out.append(Chance(actions.Take(e.id, e.code), e.pos, 1, heal * value.health(), f"eat {e.code}"))
        return out


class Signs:
    """A sign not read yet: the world tells you how to progress through text (Manual §16)."""

    def find(self, ctx: Ctx) -> list[Chance]:
        s, mem = ctx.state, ctx.memory
        worth = CLUE_WORTH * ctx.levers.curiosity_weight
        return [Chance(actions.Read(*p), p, READ_REACH, worth, "read a sign")
                for p in s.signs() if not mem.was_read(s.map_id, p)]


class Helpers:
    """An NPC that stays put and has not been greeted: likely a helper with a line (Manual §11 Speech)."""

    def find(self, ctx: Ctx) -> list[Chance]:
        s, mem = ctx.state, ctx.memory
        worth = CLUE_WORTH * ctx.levers.curiosity_weight
        if not s.speech_ready:
            return []
        return [Chance(actions.Say(e.id, e.code), e.pos, SAY_REACH, worth, f"say hello to {e.code}")
                for e in s.npcs() if not s.is_threat(e) and not s.fights(e) and not mem.heard(s.map_id, e.pos)]


class OddBlocks:
    """A block alone where it does not belong may hide a gem cache (Manual §16 Gem caches): try what we carry on it."""

    def find(self, ctx: Ctx) -> list[Chance]:
        s, mem = ctx.state, ctx.memory
        caps = travel.weapon_breaks(s)
        if not caps:
            return []
        tiles = s.world.view.tiles
        x0, y0, width, height = s.world.perception_rect()
        out = []
        for y in range(y0, y0 + height):
            for x in range(x0, x0 + width):
                p = (x, y)
                block = tiles.get(p)
                if block is None or block == "grass" or place_key(s.map_id, p) in mem.no_effect or not is_odd(tiles, p):
                    continue
                weapon = travel.weapon_for(s, block)
                if weapon is None:
                    continue
                arm = weapon if s.armed is None or not any(c.id == weapon and c.where == "armed" for c in s.carried) else None
                out.append(Chance(actions.UseOn(x, y, arm, f"try the odd {block}"), p, 1,
                                  ODD_BLOCK_WORTH * ctx.levers.greed_weight, f"try the odd {block}"))
        return out


class DeathChest:
    """Our death chest: what it holds, at the gems each item cost (Manual §11 Death: anyone may empty it first)."""

    def find(self, ctx: Ctx) -> list[Chance]:
        death = ctx.memory.last_death()
        s = ctx.state
        if death is None or death.chest_id is None or death.chest_map != s.map_id:
            return []
        worth = sum(value.item(c) for c in death.items)
        if worth <= 0:
            return []
        return [Chance(actions.Withdraw(death.chest_id), (death.chest_x, death.chest_y), 1, worth, "take back our death chest")]


class Chests:
    """A ground chest that is not ours (Manual §6, §16): from afar, a look inside, by curiosity; in reach, what it
    holds, at the gems each item costs, taken by a withdrawal of everything that fits. Looked into once, it is
    left unless it shows something worth taking."""

    def find(self, ctx: Ctx) -> list[Chance]:
        s, mem = ctx.state, ctx.memory
        death = mem.last_death()
        ours = death.chest_id if death is not None else None
        out = []
        for e in s.chests():
            if e.id == ours or place_key(s.map_id, e.pos) in mem.no_effect:
                continue  # DeathChest prices ours; a withdrawal refused here is not tried again
            held = s.world.chest_contents.get(e.id)
            if held is not None:
                worth = sum(value.item(x.code) for x in held) * ctx.levers.greed_weight
                why = "take what the chest holds"
            elif place_key(s.map_id, e.pos) in mem.chests_seen:
                continue
            else:
                worth, why = CHEST_LOOK_WORTH * ctx.levers.curiosity_weight, "look in a chest"
            out.append(Chance(actions.Withdraw(e.id), e.pos, 1, worth, why))
        return out


ALL = [Gems(), Hearts(), Loot(), Food(), Signs(), Helpers(), OddBlocks(), DeathChest(), Chests()]


def chances(ctx: Ctx) -> list[Chance]:
    out: list[Chance] = []
    for kind in ALL:
        out += kind.find(ctx)
    return [c for c in out if c.worth > 0]


def near_first(ctx: Ctx, found: list[Chance], take: int = 6) -> list[Chance]:
    """The few worth pricing a path for: best worth per block of distance."""
    here = ctx.state.pos
    return sorted(found, key=lambda c: -c.worth / (1 + max(0, chebyshev(here, c.at) - c.reach)))[:take]
