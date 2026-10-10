"""Runtime directives file (A8): params, never_attack, optional strategist fields."""

from __future__ import annotations

import logging
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .world import Entity

log = logging.getLogger(__name__)

# Defaults and ranges from docs/PLAYABLE_AGENT_PLAN.md (Runtime directives).
PARAM_DEFAULTS: dict[str, float | int] = {
    "fight_margin": 1.5,
    "retreat_hits": 2,
    "curiosity": 0.2,
    "lives_floor": 3,
    "risk": 0.5,
    "potion_reserve": 2,  # planner-only (A61): no state reads it; the planner buys below it
}


def _valid_param(name: str, value: object) -> float | int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if name in ("retreat_hits", "lives_floor", "potion_reserve"):
        if isinstance(value, float) and not value.is_integer():
            return None
        iv = int(value)
        if iv != value:
            return None
        if name == "potion_reserve" and iv < 0:
            return None
        if name in ("retreat_hits", "lives_floor") and iv < 1:
            return None
        return iv
    if name == "fight_margin":
        if value < 1:
            return None
        return float(value)
    if name == "curiosity" or name == "risk":
        if value < 0 or value > 1:
            return None
        return float(value)
    return None


@dataclass
class Directives:
    params: dict[str, float | int] = field(default_factory=lambda: dict(PARAM_DEFAULTS))
    never_attack: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    instructions: str = ""


def default_directives() -> Directives:
    return Directives(params=dict(PARAM_DEFAULTS))


def load_directives(path: Path) -> Directives:
    """Load and validate directives. Missing file yields defaults."""
    if not path.is_file():
        return default_directives()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    d = default_directives()
    params_in = raw.get("params")
    if params_in is not None:
        if not isinstance(params_in, dict):
            log.warning("%s: `params` must be a table; using defaults", path.name)
        else:
            for key, value in params_in.items():
                if key not in PARAM_DEFAULTS:
                    log.warning("%s: unknown param `%s` ignored", path.name, key)
                    continue
                ok = _valid_param(key, value)
                if ok is None:
                    log.warning(
                        "%s: param `%s`=%r out of range; keeping default %s",
                        path.name,
                        key,
                        value,
                        PARAM_DEFAULTS[key],
                    )
                    continue
                d.params[key] = ok
    never = raw.get("never_attack")
    if never is not None:
        if not isinstance(never, list) or not all(isinstance(x, str) for x in never):
            log.warning("%s: `never_attack` must be a list of strings; ignoring", path.name)
        else:
            d.never_attack = [x.strip() for x in never if x.strip()]
    goals = raw.get("goals")
    if goals is not None:
        if not isinstance(goals, list) or not all(isinstance(x, str) for x in goals):
            log.warning("%s: `goals` must be a list of strings; ignoring", path.name)
        else:
            d.goals = list(goals)
    instructions = raw.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            log.warning("%s: `instructions` must be a string; ignoring", path.name)
        else:
            d.instructions = instructions
    return d


def attack_forbidden(entity: Entity, never_attack: list[str]) -> bool:
    """True when this entity must not be attacked per directives."""
    if not never_attack:
        return False
    blocked = set(never_attack)
    if entity.kind == "character" and "character" in blocked:
        return True
    if entity.kind == "npc" and entity.code and entity.code in blocked:
        return True
    return False


def use_blocked_by_never_attack(intent: dict, entities: list[Entity], never_attack: list[str]) -> bool:
    """Executor guard: drop a Use intent aimed at a forbidden target.

    A Use on yourself (``{"kind": "self"}``: Heal eating or drinking, A10) is
    never an attack, so it passes even with `character` in never_attack.
    """
    if intent.get("verb") != "Use" or not never_attack:
        return False
    target = intent.get("target") or {}
    kind = target.get("kind")
    if kind == "character":
        cid = target.get("character_id")
        for e in entities:
            if e.kind == "character" and e.id == cid:
                return attack_forbidden(e, never_attack)
        return "character" in never_attack
    if kind == "npc":
        nid = target.get("npc_id")
        for e in entities:
            if e.kind == "npc" and e.id == nid:
                return attack_forbidden(e, never_attack)
        # The server finds the NPC at run time; one we cannot name may be forbidden.
        return True
    if kind == "block":
        x, y = target.get("x"), target.get("y")
        if x is None or y is None:
            return False
        pos = (int(x), int(y))
        for e in entities:
            if e.kind == "npc" and e.pos == pos:
                return attack_forbidden(e, never_attack)
        return False
    return False


def _file_sig(st: os.stat_result) -> tuple[int, int, int]:
    """What identifies one version of the file: mtime, size, inode."""
    return (st.st_mtime_ns, st.st_size, st.st_ino)


@dataclass
class DirectivesWatch:
    """Re-read the directives file when its mtime, size, or inode changes.

    A file that fails to read or parse keeps the last good directives
    (defaults on first load) and is retried once any of those differ from
    the failed version, even when the mtime matches the last good load.
    Deleting the file restores the defaults.

    ``pinned_goals`` go on top of the file's ``goals`` at every load, with
    or without a file: how a program (the M7 smoke script) sets a directives
    goal without writing the user's file. Each is pinned once: :meth:`unpin`
    drops it when its op is done, so a later reload never brings it back.
    """

    path: Path
    pinned_goals: list[str] = field(default_factory=list)
    directives: Directives = field(default_factory=default_directives)
    _sig: tuple[int, int, int] | None = field(default=None, repr=False)
    _bad_sig: tuple[int, int, int] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.directives = self._pin(self.directives)

    def unpin(self, goal: str) -> None:
        """Drop a pinned goal for good (its op is done or dropped, or its target
        given up), here and from the directives in force."""
        if goal in self.pinned_goals:
            self.pinned_goals.remove(goal)
            self.directives.goals = [g for g in self.directives.goals if g != goal]

    def _pin(self, d: Directives) -> Directives:
        d.goals = list(self.pinned_goals) + [g for g in d.goals if g not in self.pinned_goals]
        return d

    def maybe_reload(self) -> bool:
        """Load when the file is new or changed. Returns True when directives updated."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._bad_sig = None
            if self._sig is None:
                return False
            self._sig = None
            self.directives = self._pin(default_directives())
            return True
        sig = _file_sig(st)
        if sig == self._sig or sig == self._bad_sig:
            return False
        try:
            loaded = load_directives(self.path)
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("%s: %s; keeping the last good directives", self.path.name, e)
            self._bad_sig = sig
            return False
        self._sig, self._bad_sig = sig, None
        self.directives = self._pin(loaded)
        return True

    def ensure_loaded(self) -> None:
        if self._sig is None and self.path.is_file():
            self.maybe_reload()
