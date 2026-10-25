"""Lou's need kinds: what beating Agent Realm requires, by the game's rules (docs/v3/SPEC.md **The needs graph**).

The kinds come from the Manual: a world is beaten by clearing every level
(§3); a level is behind an entrance door, which may be locked or hidden
(§11 Bosses and levels, Breaking blocks); clearing it means beating its boss
in time; fights are decided by gear and health (§11 Combat); gear is bought
with gems where it is sold (§11 Shops, §16); gems come from field work,
kills and caches (§16). No need names an item or a place: which entrance to
take first, what gear a level needs and where it is sold are the
strategist's judgments (``ctx.judgments``), read here.

A need's key is stable across decisions, so blocks and judgments attach to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_adk import supplies
from agentrealm_adk.world import DOORS, chebyshev

from adk3.core.needs import Check
from adk3.realm.context import Ctx
from adk3.realm.memory import Entrance

from . import skills


def judged(ctx: Ctx, kind: str, key: str):
    """The strategist's judgment of ``kind`` for ``key``, or None."""
    return (ctx.judgments.get(kind) or {}).get(key)


@dataclass(frozen=True)
class BeatTheWorld:
    """Clear every level the world authored (Manual §3: that transcends the character)."""

    key = "world"
    title = "beat the world: clear every level"

    def check(self, ctx: Ctx) -> Check:
        s = ctx.state
        if s.level_count and len(s.levels_cleared) >= s.level_count:
            return Check.met()
        return Check.unmet(f"{len(s.levels_cleared)} of {s.level_count or '?'} levels cleared")

    def parts(self, ctx: Ctx) -> list:
        out: list = [StayAlive()]
        chosen = list(ctx.judgments.get("order") or [])
        known = list(ctx.memory.entrances)
        ordered = [k for k in chosen if k in ctx.memory.entrances] + sorted(
            (k for k in known if k not in chosen), key=lambda k: _distance(ctx, ctx.memory.entrances[k]))
        out += [ClearLevel(k) for k in ordered]
        if not known:
            out.append(KnowEntrances())
        out.append(LearnTheWorld())
        return out

    def skill(self, ctx: Ctx):
        return None


@dataclass(frozen=True)
class KnowEntrances:
    """Where the level entrances are: the minimap marks every one once its map is revealed (Manual §5.3)."""

    key = "know:entrances"
    title = "find where the level entrances are"

    def check(self, ctx: Ctx) -> Check:
        return Check.met() if ctx.memory.entrances else Check.unknown("no minimap marks read yet")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        return skills.Seek("find the level entrances", lambda c: bool(c.memory.entrances))


@dataclass(frozen=True)
class LearnTheWorld:
    """Fallback while nothing above can be worked: see more of the world, where clues and shops are."""

    key = "know:world"
    title = "see more of the world"

    def check(self, ctx: Ctx) -> Check:
        return Check.unmet("there is unseen ground")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        toward = judged(ctx, "explore", "toward")
        return skills.Seek("see more of the world", lambda c: False, tuple(toward) if toward else None)


@dataclass(frozen=True)
class ClearLevel:
    """The level behind one entrance: ready for it, reach its boss, beat the boss (Manual §11 Bosses and levels)."""

    entrance: str  # the entrance's place key
    ordered = True  # ready, then through the door, then the boss

    @property
    def key(self) -> str:
        return f"clear:{self.entrance}"

    @property
    def title(self) -> str:
        return f"clear the level behind {self.entrance}"

    def check(self, ctx: Ctx) -> Check:
        e = ctx.memory.entrances.get(self.entrance)
        if e is not None and e.level is not None and e.level in ctx.state.levels_cleared:
            return Check.met(f"level {e.level} cleared")
        return Check.unmet("not cleared")

    def parts(self, ctx: Ctx) -> list:
        return [ReadyFor(self.entrance), ReachLevel(self.entrance), BeatBoss(self.entrance)]

    def skill(self, ctx: Ctx):
        return None


@dataclass(frozen=True)
class ReadyFor:
    """Carry and wear the gear and potions the strategist judged this level needs."""

    entrance: str

    @property
    def key(self) -> str:
        return f"ready:{self.entrance}"

    @property
    def title(self) -> str:
        return f"be ready for the level behind {self.entrance}"

    def needs(self, ctx: Ctx) -> dict | None:
        return judged(ctx, "needs", self.entrance)

    def check(self, ctx: Ctx) -> Check:
        if _inside(ctx, self.entrance):
            return Check.met("already inside")
        need = self.needs(ctx)
        if need is None:
            return Check.unknown("the strategist has not judged what this level needs")
        missing = [p.title for p in self.parts(ctx) if p.check(ctx).status.value != "met"]
        return Check.unmet("missing: " + ", ".join(missing)) if missing else Check.met("ready")

    def parts(self, ctx: Ctx) -> list:
        need = self.needs(ctx) or {}
        out: list = []
        for code in need.get("items") or []:
            out += [Have(code), Equipped(code)] if _equippable(code) else [Have(code)]
        potion = need.get("potion")
        for i in range(int(need.get("potions") or 0)):
            out.append(Have(potion or "small_potion", count=i + 1))
        return out

    def skill(self, ctx: Ctx):
        return None


@dataclass(frozen=True)
class ReachLevel:
    """Get through the entrance door: travel to it, open it if it is hidden or locked (Manual §11)."""

    entrance: str

    @property
    def key(self) -> str:
        return f"reach:{self.entrance}"

    @property
    def title(self) -> str:
        return f"get through the entrance at {self.entrance}"

    def check(self, ctx: Ctx) -> Check:
        if _inside(ctx, self.entrance):
            return Check.met("inside")
        e = ctx.memory.entrances.get(self.entrance)
        if e is not None and e.refused:
            return Check.unmet(f"it refused us: {e.refused}")
        return Check.unmet("not through it")

    def parts(self, ctx: Ctx) -> list:
        e = ctx.memory.entrances.get(self.entrance)
        if e is None or (not e.locked and not _hidden(e)):
            return []
        opener = (judged(ctx, "doors", self.entrance) or {}).get("opener")
        return [Have(opener)] if opener else [KnowOpener(self.entrance)]

    def skill(self, ctx: Ctx):
        e = ctx.memory.entrances.get(self.entrance)
        if e is None:
            return None
        if _hidden(e):
            opener = (judged(ctx, "doors", self.entrance) or {}).get("opener")
            return skills.BreakOpen(e.map_id, (e.x, e.y), opener) if opener else None
        return skills.Travel(e.map_id, (e.x, e.y), f"through the entrance at {e.x},{e.y}", door=True)


@dataclass(frozen=True)
class KnowOpener:
    """What opens a locked or hidden entrance: a clue or a judgment (Manual §16 Rumor helpers)."""

    entrance: str

    @property
    def key(self) -> str:
        return f"know:opener:{self.entrance}"

    @property
    def title(self) -> str:
        return f"learn what opens the entrance at {self.entrance}"

    def check(self, ctx: Ctx) -> Check:
        found = (judged(ctx, "doors", self.entrance) or {}).get("opener")
        return Check.met(found) if found else Check.unknown("no clue says yet")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        return skills.Seek("look for clues about this entrance", lambda c: self.check(c).status.value == "met")


@dataclass(frozen=True)
class BeatBoss:
    """Inside the level: find the boss and beat it before its clock runs out."""

    entrance: str

    @property
    def key(self) -> str:
        return f"beat:{self.entrance}"

    @property
    def title(self) -> str:
        return f"beat the boss behind {self.entrance}"

    def check(self, ctx: Ctx) -> Check:
        e = ctx.memory.entrances.get(self.entrance)
        if e is not None and e.level in ctx.state.levels_cleared:
            return Check.met()
        return Check.unmet("boss alive")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        s = ctx.state
        if not s.in_level:
            return None
        return skills.FightBoss(s.level) if s.boss() is not None else skills.FindBoss(s.level)


@dataclass(frozen=True)
class Have:
    """Carry an item: buy it where it is sold, or find out where it is sold (Manual §11 Shops)."""

    code: str
    count: int = 1

    @property
    def key(self) -> str:
        return f"have:{self.code}" + (f":{self.count}" if self.count > 1 else "")

    @property
    def title(self) -> str:
        return f"have {self.code}" + (f" x{self.count}" if self.count > 1 else "")

    def check(self, ctx: Ctx) -> Check:
        n = sum(1 for c in ctx.state.carried if c.code == self.code)
        return Check.met() if n >= self.count else Check.unmet(f"carry {n}")

    def seller(self, ctx: Ctx):
        sellers = ctx.memory.sellers(self.code)
        s = ctx.state
        return min(sellers, key=lambda x: (x.map_id != s.map_id, chebyshev((x.x, x.y), s.pos))) if sellers else None

    def parts(self, ctx: Ctx) -> list:
        seller = self.seller(ctx)
        if seller is None:
            return [KnowSeller(self.code)]
        return [Gems(seller.price)]

    def skill(self, ctx: Ctx):
        seller = self.seller(ctx)
        return skills.Buy(self.code, seller) if seller is not None else None


@dataclass(frozen=True)
class Equipped:
    """Arm a weapon or wear armor we carry: carried, it does nothing (Manual §11 Armed, worn, or only carried)."""

    code: str

    @property
    def key(self) -> str:
        return f"equip:{self.code}"

    @property
    def title(self) -> str:
        return f"equip {self.code}"

    def check(self, ctx: Ctx) -> Check:
        on = any(c.code == self.code and c.where in ("armed", "worn") for c in ctx.state.carried)
        return Check.met() if on else Check.unmet("carried, not equipped")

    def parts(self, ctx: Ctx) -> list:
        return [Have(self.code)]

    def skill(self, ctx: Ctx):
        return skills.Equip(self.code)


@dataclass(frozen=True)
class KnowSeller:
    """Where an item is sold: seen on a shelf, or judged from the Manual and clues and then looked for."""

    code: str

    @property
    def key(self) -> str:
        return f"know:seller:{self.code}"

    @property
    def title(self) -> str:
        return f"find where {self.code} is sold"

    def check(self, ctx: Ctx) -> Check:
        return Check.met() if ctx.memory.sellers(self.code) else Check.unknown("not seen for sale")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        where = judged(ctx, "where", self.code) or {}
        found = lambda c: bool(c.memory.sellers(self.code))  # noqa: E731
        if {"map", "x", "y"} <= where.keys():
            return skills.Seek(f"look where {self.code} is likely sold", found, (int(where["x"]), int(where["y"])))
        return skills.Seek(f"look for {self.code} for sale", found)


@dataclass(frozen=True)
class Gems:
    """Hold at least ``n`` gems: field work (Manual §16 How to make money)."""

    n: int

    @property
    def key(self) -> str:
        return f"gems:{self.n}"

    @property
    def title(self) -> str:
        return f"hold {self.n} gems"

    def check(self, ctx: Ctx) -> Check:
        g = ctx.state.gems
        return Check.met() if g >= self.n else Check.unmet(f"hold {g}")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        return skills.EarnGems(self.n)


@dataclass(frozen=True)
class StayAlive:
    """Health enough for what comes next: health never comes back on its own (Manual §11 Food and potions)."""

    key = "alive"
    title = "stay alive"

    def floor(self, ctx: Ctx) -> float:
        return ctx.levers.heal_below * ctx.state.max_health

    def check(self, ctx: Ctx) -> Check:
        s = ctx.state
        if s.health > self.floor(ctx) or s.on_safe_ground and not s.threats():
            return Check.met(f"health {s.health}/{s.max_health}")
        return Check.unmet(f"health {s.health}/{s.max_health}")

    def parts(self, ctx: Ctx) -> list:
        return []

    def skill(self, ctx: Ctx):
        s = ctx.state
        if any(c.code in _POTIONS for c in s.carried):
            return None  # the combat engine and the drink opportunity spend potions
        if ctx.town is not None:
            map_id, cell = ctx.town
            return skills.Travel(map_id, cell, "back to town to be safe while hurt")
        return None


_POTIONS = ("small_potion", "potion", "large_potion")


def _distance(ctx: Ctx, e: Entrance) -> int:
    s = ctx.state
    return chebyshev((e.x, e.y), s.pos) if e.map_id == s.map_id else 10_000


def _inside(ctx: Ctx, entrance: str) -> bool:
    """We stand inside the level this entrance opens."""
    e = ctx.memory.entrances.get(entrance)
    s = ctx.state
    if e is None or not s.in_level:
        return False
    return e.level == s.level or e.leads_to == s.map_id


def _hidden(e: Entrance) -> bool:
    """The mark is on a block, not a door: a hidden entrance (Manual §11 Bosses and levels)."""
    return bool(e.block) and e.block not in DOORS


def _equippable(code: str) -> bool:
    row = supplies.row(code)
    return row is not None and row.supply_class in ("weapon", "armor", "accessory")

