"""Behavior profiles (TOML) and local trace paths keyed by profile plus character id."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

POLICY_KINDS = ("idle", "wander", "scripted")
GOALS = ("explore", "doors", "goto")  # built-in planner inputs (plan.builtin_goals)
ON_HOSTILE = ("flee", "fight", "ignore")
HOSTILE_KINDS = ("npc", "character")

# Traces and the shared world knowledge base live here (gitignored).
STATE_DIR = Path(__file__).resolve().parent.parent / ".state"


@dataclass
class Policy:
    kind: str = "scripted"
    goals: list[str] = field(default_factory=lambda: ["explore"])
    goto: tuple[int, int] | None = None
    goto_map: int | None = None  # when set, goto targets this map (A26)
    on_hostile: str = "flee"
    hostile: list[str] = field(default_factory=lambda: ["npc"])
    hostile_range: int = 2
    pickup: bool = True
    avoid_blocks: list[str] = field(default_factory=lambda: ["fire", "lava"])
    entity_refresh: int = 5
    seed: int | None = None


@dataclass
class CharacterConfig:
    """A behavior profile: policy and world. The file stem is ``profile``."""

    profile: str
    world: str
    policy: Policy
    path: Path

    def trace_path(self, character_id: int) -> Path:
        return STATE_DIR / f"{self.profile}.{character_id}.trace.jsonl"

    @property
    def directives_path(self) -> Path:
        """``characters/<profile>.directives.toml`` beside the profile file (A8)."""
        return self.path.parent / f"{self.profile}.directives.toml"


class ConfigError(ValueError):
    pass


def load(path: str | Path) -> CharacterConfig:
    path = Path(path).resolve()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    for legacy in ("name", "avatar", "model_agent"):
        if legacy in raw:
            raise ConfigError(
                f"{path.name}: `{legacy}` belongs on `create`, not in the profile file (A59)"
            )
    pol = raw.get("policy", {})
    policy = Policy()
    for key, value in pol.items():
        if not hasattr(policy, key):
            raise ConfigError(f"{path.name}: unknown policy key `{key}`")
        setattr(policy, key, value)
    for key in ("goals", "hostile", "avoid_blocks"):
        value = getattr(policy, key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{path.name}: `policy.{key}` must be a list of strings, like [\"{Policy().__dict__[key][0]}\"]")
    for key in ("hostile_range", "entity_refresh"):
        value = getattr(policy, key)
        if type(value) is not int or value < 0:
            raise ConfigError(f"{path.name}: `policy.{key}` must be a whole number >= 0")
    if type(policy.pickup) is not bool:
        raise ConfigError(f"{path.name}: `policy.pickup` must be true or false")
    if policy.seed is not None and type(policy.seed) is not int:
        raise ConfigError(f"{path.name}: `policy.seed` must be an integer")
    if policy.goto_map is not None and type(policy.goto_map) is not int:
        raise ConfigError(f"{path.name}: `policy.goto_map` must be an integer")
    if not isinstance(raw.get("world", "sandbox"), str):
        raise ConfigError(f"{path.name}: `world` must be a string")
    if policy.goto is not None:
        if not isinstance(policy.goto, list) or len(policy.goto) != 2 or not all(type(v) is int for v in policy.goto):
            raise ConfigError(f"{path.name}: `policy.goto` must be [x, y]")
        policy.goto = tuple(policy.goto)  # type: ignore[assignment]
    _check(path, "policy.kind", [policy.kind], POLICY_KINDS)
    _check(path, "policy.goals", policy.goals, GOALS)
    _check(path, "policy.on_hostile", [policy.on_hostile], ON_HOSTILE)
    _check(path, "policy.hostile", policy.hostile, HOSTILE_KINDS)
    if "goto" in policy.goals and policy.goto is None:
        raise ConfigError(f"{path.name}: goal `goto` needs `policy.goto = [x, y]`")
    if policy.goto_map is not None and policy.goto is None:
        raise ConfigError(f"{path.name}: `policy.goto_map` needs `policy.goto = [x, y]`")
    return CharacterConfig(
        profile=path.stem,
        world=raw.get("world", "sandbox"),
        policy=policy,
        path=path,
    )


def _check(path: Path, key: str, values: list, allowed: tuple) -> None:
    for v in values:
        if v not in allowed:
            raise ConfigError(f"{path.name}: `{key}` has `{v}`; allowed: {', '.join(allowed)}")
