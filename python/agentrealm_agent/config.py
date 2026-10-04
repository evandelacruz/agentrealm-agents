"""Character files (TOML) and the per-character state file."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

POLICY_KINDS = ("idle", "wander", "scripted")
GOALS = ("explore", "doors", "goto", "hold", "wander")
ON_HOSTILE = ("flee", "fight", "ignore")
HOSTILE_KINDS = ("npc", "character")

# Created character IDs and traces, next to the package (gitignored).
STATE_DIR = Path(__file__).resolve().parent.parent / ".state"


@dataclass
class Policy:
    kind: str = "scripted"
    goals: list[str] = field(default_factory=lambda: ["explore"])
    goto: tuple[int, int] | None = None
    on_hostile: str = "flee"
    hostile: list[str] = field(default_factory=lambda: ["npc"])
    hostile_range: int = 2
    pickup: bool = True
    avoid_blocks: list[str] = field(default_factory=lambda: ["fire", "lava"])
    entity_refresh: int = 5
    seed: int | None = None


@dataclass
class CharacterConfig:
    name: str
    avatar: str
    model_agent: str
    world: str
    policy: Policy
    path: Path

    @property
    def state_path(self) -> Path:
        return STATE_DIR / f"{self.name}.json"

    @property
    def trace_path(self) -> Path:
        return STATE_DIR / f"{self.name}.trace.jsonl"


class ConfigError(ValueError):
    pass


def load(path: str | Path) -> CharacterConfig:
    path = Path(path).resolve()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    for key in ("name", "avatar", "model_agent"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ConfigError(f"{path.name}: `{key}` is required")
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
    return CharacterConfig(
        name=raw["name"].strip(),
        avatar=raw["avatar"].strip(),
        model_agent=raw["model_agent"].strip(),
        world=raw.get("world", "sandbox"),
        policy=policy,
        path=path,
    )


def _check(path: Path, key: str, values: list, allowed: tuple) -> None:
    for v in values:
        if v not in allowed:
            raise ConfigError(f"{path.name}: `{key}` has `{v}`; allowed: {', '.join(allowed)}")


def load_state(cfg: CharacterConfig) -> dict | None:
    try:
        return json.loads(cfg.state_path.read_text())
    except FileNotFoundError:
        return None


def save_state(cfg: CharacterConfig, state: dict) -> None:
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text(json.dumps(state, indent=2) + "\n")
