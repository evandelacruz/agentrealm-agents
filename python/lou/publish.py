"""The strategist's answer as something the executive can carry out (docs/v3/SPEC.md **The strategist**).

An answer names a goal by a **test** from a fixed menu, so the executive
can check it every decision, and each approach by a **method** from a fixed
menu with its settings, its failure conditions and its tactics. Anything
off the menus, or missing what it needs, is refused with why, and the
strategist is told.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from agentrealm_adk.world import Pos, chebyshev

from adk3.core.executive import Publication
from adk3.realm.context import Ctx
from adk3.realm.memory import Sighting

from . import skills
from .skills import seen

DEFAULT_PATIENCE = 60.0  # seconds with no progress before an approach has failed, when the levers set none


# Goals: a test the executive checks every decision.

@dataclass(frozen=True)
class Goal:
    text: str
    test: str
    args: dict = field(default_factory=dict, hash=False)

    def met(self, ctx: Ctx) -> bool:
        s, a = ctx.state, self.args
        if self.test == "gems":
            return s.gems >= int(a["n"])
        if self.test == "carry":
            return sum(1 for c in s.carried if c.code == a["item"]) >= int(a.get("count", 1))
        if self.test == "equipped":
            return any(c.code == a["item"] and c.where in ("armed", "worn") for c in s.carried)
        if self.test == "cleared":
            return int(a["level"]) in s.levels_cleared
        if self.test == "inside":
            e = ctx.memory.entrances.get(a["entrance"])
            return s.in_level and e is not None and (e.leads_to == s.map_id or (e.level is not None and e.level == s.level))
        if self.test == "at":
            return s.map_id == int(a.get("map", s.map_id)) and chebyshev(s.pos, (int(a["x"]), int(a["y"]))) <= 1
        if self.test == "health":
            return s.health >= int(a["n"])
        if self.test == "seller":
            return bool(ctx.memory.sellers(a["item"]))
        if self.test == "found":
            return seen(s, ctx.memory, a["what"])
        if self.test == "boss_seen":
            return s.boss() is not None
        raise ValueError(f"unknown goal test {self.test!r}")


GOAL_TESTS = {
    "gems": ["n"], "carry": ["item"], "equipped": ["item"], "cleared": ["level"], "inside": ["entrance"],
    "at": ["x", "y"], "health": ["n"], "seller": ["item"], "found": ["what"], "boss_seen": [],
}


# Approaches: a method with its settings, failure conditions and tactics.

@dataclass
class Approach:
    text: str
    method: str
    args: dict
    fails_if: dict
    tactics: dict

    error: str = ""

    def skill(self, ctx: Ctx):
        """The method's skill; None, with ``error`` saying why, when it cannot start (no shelf seen, no chest)."""
        try:
            return self.make(ctx)
        except (ValueError, KeyError, TypeError) as e:
            self.error = str(e)
            return None

    def make(self, ctx: Ctx):
        a, s = self.args, ctx.state
        m = self.method
        if m == "earn_gems":
            return skills.EarnGems(_cell(a) if "x" in a else None)
        if m == "buy":
            return skills.Buy(a["item"], _seller(ctx, a))
        if m == "equip":
            return skills.Equip(a["item"])
        if m == "drink":
            return skills.DrinkPotion(a.get("item"))
        if m == "travel":
            return skills.Travel(int(a.get("map", s.map_id)), _cell(a), a.get("why", "travel"), bool(a.get("door")))
        if m == "explore":
            return skills.Seek(a.get("why", "explore"), lambda c: False, _cell(a) if "x" in a else None)
        if m == "break_open":
            return skills.BreakOpen(int(a.get("map", s.map_id)), _cell(a), a["item"])
        if m == "forage":
            return skills.Forage()
        if m == "recover":
            death = ctx.memory.last_death()
            if death is None or death.chest_id is None:
                raise ValueError("no death chest to recover")
            return skills.Recover(death)
        if m == "find_boss":
            return skills.FindBoss()
        if m == "fight_boss":
            return skills.FightBoss()
        raise ValueError(f"unknown method {m!r}")

    def failed(self, ctx: Ctx, skill, seconds: float, stalled: float) -> str | None:
        if skill is None:
            return f"it could not start: {self.error}"
        f = self.fails_if
        if "minutes" in f and seconds >= float(f["minutes"]) * 60:
            return f"its {f['minutes']} minutes ran out"
        patience = float(f.get("no_progress_seconds", ctx.levers.patience or DEFAULT_PATIENCE))
        if stalled >= patience:
            return f"no progress for {patience:.0f}s"
        held = ctx.memory.held_up
        if held is not None and time.time() - held[0] <= seconds:
            return f"held up: {held[1]}"
        if "no_gem_minutes" in f and isinstance(skill, skills.EarnGems) and skill.minutes_since_gem(ctx) >= float(f["no_gem_minutes"]):
            return f"no gem in {f['no_gem_minutes']} minutes"
        return skill.failed(ctx)


METHODS = {
    "earn_gems": [], "buy": ["item"], "equip": ["item"], "drink": [], "travel": ["x", "y"], "explore": [],
    "break_open": ["x", "y", "item"], "forage": [], "recover": [], "find_boss": [], "fight_boss": [],
}


def _cell(a: dict) -> Pos:
    return int(a["x"]), int(a["y"])


def _seller(ctx: Ctx, a: dict) -> Sighting:
    """The shelf named, or the nearest known one selling the item."""
    sellers = ctx.memory.sellers(a["item"])
    if not sellers:
        raise ValueError(f"no shelf selling {a['item']} has been seen")
    if "x" in a:
        return min(sellers, key=lambda x: chebyshev((x.x, x.y), _cell(a)))
    s = ctx.state
    return min(sellers, key=lambda x: (x.map_id != s.map_id, chebyshev((x.x, x.y), s.pos)))


# Reading an answer.

def publication(answer: dict[str, Any], ctx: Ctx) -> Publication:
    """The answer as a publication; ValueError with why when it cannot be carried out."""
    goal = _goal(answer.get("goal"))
    approach = _approach(answer.get("approach"), "approach", ctx)
    if approach.skill(ctx) is None:  # plan A must be able to start now; plan B and next are checked when they start
        raise ValueError(f"the approach cannot start: {approach.error}")
    plan_b = _approach(answer["plan_b"], "plan_b", ctx) if answer.get("plan_b") else None
    nxt = answer.get("next")
    following = None
    if isinstance(nxt, dict) and nxt.get("goal") and nxt.get("approach"):
        following = Publication(_goal(nxt["goal"]), str(nxt.get("why", "")), _approach(nxt["approach"], "next", ctx))
    return Publication(goal, str(answer.get("why", "")), approach, plan_b, following)


def _goal(raw: Any) -> Goal:
    if not isinstance(raw, dict) or raw.get("test") not in GOAL_TESTS:
        raise ValueError(f"goal needs a test from: {', '.join(GOAL_TESTS)}")
    missing = [k for k in GOAL_TESTS[raw["test"]] if k not in raw]
    if missing:
        raise ValueError(f"goal test {raw['test']} needs {', '.join(missing)}")
    args = {k: v for k, v in raw.items() if k not in ("test", "text")}
    return Goal(str(raw.get("text") or raw["test"]), raw["test"], args)


def _approach(raw: Any, name: str, ctx: Ctx) -> Approach:
    if not isinstance(raw, dict) or raw.get("method") not in METHODS:
        raise ValueError(f"{name} needs a method from: {', '.join(METHODS)}")
    missing = [k for k in METHODS[raw["method"]] if k not in raw]
    if missing:
        raise ValueError(f"{name} method {raw['method']} needs {', '.join(missing)}")
    args = {k: v for k, v in raw.items() if k not in ("method", "text", "fails_if", "tactics")}
    approach = Approach(str(raw.get("text") or raw["method"]), raw["method"], args,
                        dict(raw.get("fails_if") or {}), dict(raw.get("tactics") or {}))
    return approach
