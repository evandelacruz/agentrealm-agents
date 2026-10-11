"""Per-world knowledge base at ``<STATE_DIR>/worlds/<world_code>.json`` (``AGENTREALM_STATE_DIR``, default python/.state).

Shared by every character run from this checkout that plays the same world.
Sections are filled over later milestones; A18 owns ``items``.

`run` loads each world's file once at start and saves it once at exit. All
characters of a world share the one in-memory object; anything that changes
it holds `kb.lock`, and `save` takes the same lock. One `run` process per
world at a time: two processes on the same world each save their own copy
at exit, and the last save wins.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import STATE_DIR

WORLDS_DIR = STATE_DIR / "worlds"
SCHEMA_VERSION = 1

# World codes come from the API; keep paths safe.
_WORLD_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

SECTION_KEYS = (
    "maps",
    "clues",
    "breaks",
    "npc_types",
    "items",
    "entrances",
    "levels",
    "compose",
)


class KnowledgeBaseError(ValueError):
    pass


def world_path(world_code: str) -> Path:
    _check_world_code(world_code)
    return WORLDS_DIR / f"{world_code}.json"


def _check_world_code(world_code: str) -> None:
    if not isinstance(world_code, str) or not _WORLD_CODE.match(world_code):
        raise KnowledgeBaseError(f"invalid world code: {world_code!r}")


@dataclass
class KnowledgeBase:
    """Sections match docs/PLAYABLE_AGENT_PLAN.md **Knowledge base**."""

    world_code: str
    schema_version: int = SCHEMA_VERSION
    maps: dict[str, Any] = field(default_factory=dict)
    clues: list[dict[str, Any]] = field(default_factory=list)
    breaks: dict[str, dict[str, Any]] = field(default_factory=dict)
    npc_types: dict[str, dict[str, Any]] = field(default_factory=dict)
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    entrances: dict[str, dict[str, Any]] = field(default_factory=dict)
    levels: dict[str, dict[str, Any]] = field(default_factory=dict)
    compose: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "world_code": self.world_code,
        }
        for key in SECTION_KEYS:
            out[key] = getattr(self, key)
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, world_code: str, raw: dict[str, Any]) -> KnowledgeBase:
        _check_world_code(world_code)
        if not isinstance(raw, dict):
            raise KnowledgeBaseError("knowledge base root must be a JSON object")
        file_world = raw.get("world_code")
        if file_world is not None and file_world != world_code:
            raise KnowledgeBaseError(
                f"world_code mismatch: file has {file_world!r}, expected {world_code!r}"
            )
        version = raw.get("schema_version", SCHEMA_VERSION)
        if type(version) is not int:
            raise KnowledgeBaseError("schema_version must be an integer")
        if version > SCHEMA_VERSION:
            raise KnowledgeBaseError(
                f"unsupported schema_version {version} (agent supports {SCHEMA_VERSION})"
            )
        kb = cls(world_code=world_code, schema_version=version)
        for key in SECTION_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if key == "clues" or key == "compose":
                if not isinstance(value, list):
                    raise KnowledgeBaseError(f"{key} must be a list")
                setattr(kb, key, value)
            else:
                if not isinstance(value, dict):
                    raise KnowledgeBaseError(f"{key} must be an object")
                setattr(kb, key, value)
        known = {"schema_version", "world_code", *SECTION_KEYS}
        kb.extra = {k: v for k, v in raw.items() if k not in known}
        _migrate_entrance_keys(kb.entrances)
        return kb

    @classmethod
    def empty(cls, world_code: str) -> KnowledgeBase:
        _check_world_code(world_code)
        return cls(world_code=world_code)


def knowledge_items(knowledge: KnowledgeBase | None) -> dict[str, dict[str, Any]]:
    """The ``items`` section (weapon stats, shop prices, armor trials), or ``{}``."""
    return (knowledge.items if knowledge else {}) or {}


def _migrate_entrance_keys(entrances: dict[str, Any]) -> None:
    """Rekey old cell-only ``"x,y"`` entrance rows to ``"<map_id>:<x>,<y>"`` from
    their ``map_id``, once at load. A row with no usable ``map_id`` keeps its old
    key; readers skip it until the next minimap read records the mark again."""
    for key in [k for k in entrances if ":" not in k]:
        row = entrances[key]
        try:
            x, y = (int(p) for p in key.split(",", 1))
            map_id = int(row["map_id"])
        except (KeyError, TypeError, ValueError):
            continue
        del entrances[key]
        entrances.setdefault(f"{map_id}:{x},{y}", row)


def load(world_code: str) -> KnowledgeBase:
    path = world_path(world_code)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return KnowledgeBase.empty(world_code)
    except json.JSONDecodeError as e:
        raise KnowledgeBaseError(f"{path}: invalid JSON: {e}") from e
    return KnowledgeBase.from_dict(world_code, raw)


def save(kb: KnowledgeBase) -> None:
    path = world_path(kb.world_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    with kb.lock:
        text = json.dumps(kb.to_dict(), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
