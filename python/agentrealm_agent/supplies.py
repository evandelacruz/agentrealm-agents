"""The Manual's Supplies reference: one row per supply subtype (A54).

The site serves the table as JSON at ``SUPPLIES_URL`` (API Supplies
reference, saims B132): per ``code`` its ``class``, ``slot``, ``use_effects``
(what ``Use`` does), ``attack_range``, ``heal``, ``used_up_on_break``,
``eaten_on_pickup``, ``gem_prices`` and more; a field that does not apply is
left out. It is a page on the website, not an API read, so it spends nothing
from a character's call budget, and it is fetched once per run (``load``).

Where the table comes from, first that works:

1. ``CACHE_PATH``, when it is younger than ``CACHE_MAX_AGE_SECONDS`` (the site
   serves it with ``max-age=3600``), so back-to-back runs fetch it once.
2. ``SUPPLIES_URL``, saved to ``CACHE_PATH`` on success.
3. ``CACHE_PATH`` at any age, when the site cannot be reached.
4. ``BUNDLED_PATH``, a copy checked in with the agent. The module starts on it,
   so tests and a run that never calls ``load`` still have every row.

The questions the agent asks of a subtype (is it food, a potion, a weapon;
what blocks it breaks; how far it reaches) are the functions below. A code
the table does not list answers "no", with the default reach of one block:
learning in play (A18, A28, A46) still covers it.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import STATE_DIR
from .item_table import absorb_supply_entry

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

SUPPLIES_URL = "https://agentrealm.gg/docs/supplies.json"
BUNDLED_PATH = Path(__file__).resolve().parent / "reference" / "supplies.json"
CACHE_PATH = STATE_DIR / "supplies.json"
CACHE_MAX_AGE_SECONDS = 3600
FETCH_TIMEOUT_SECONDS = 5.0
# The ``use_effects`` that open a block (cut, chop, smash, burn, blast); light
# and water open none (docs/GAME_NOTES.md Breaking blocks). ``plan`` takes its
# ``break_block`` capabilities from here.
CAPABILITIES = frozenset({"cut", "chop", "smash", "burn", "blast"})
# A ``Use`` on a block reaches the next block, corners included, with a tool
# or a weapon that authors no ``attack_range`` (API Use, B104).
DEFAULT_BLOCK_REACH = 1


@dataclass(frozen=True)
class Supply:
    """One row of the Supplies reference, the fields the agent reads."""

    code: str
    supply_class: str
    use_effects: frozenset[str]
    attack_range: int | None = None
    heal: int | None = None
    used_up_on_break: bool | None = None
    eaten_on_pickup: bool = False


def parse(raw: Any) -> dict[str, Supply]:
    """Rows by code from the served JSON array; malformed rows are skipped."""
    if not isinstance(raw, list):
        return {}
    out: dict[str, Supply] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        if not isinstance(code, str) or not code:
            continue
        effects = row.get("use_effects")
        used_up = row.get("used_up_on_break")
        out[code] = Supply(
            code=code,
            supply_class=row.get("class") if isinstance(row.get("class"), str) else "",
            use_effects=frozenset(e for e in effects if isinstance(e, str)) if isinstance(effects, list) else frozenset(),
            attack_range=_positive_int(row.get("attack_range")),
            heal=_positive_int(row.get("heal")),
            used_up_on_break=used_up if isinstance(used_up, bool) else None,
            eaten_on_pickup=row.get("eaten_on_pickup") is True,
        )
    return out


def _positive_int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v > 0 else None


def _read(path: Path) -> dict[str, Supply]:
    try:
        return parse(json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}


_table: dict[str, Supply] = _read(BUNDLED_PATH)


def fetch(url: str = SUPPLIES_URL, timeout: float = FETCH_TIMEOUT_SECONDS) -> Any:
    """The served JSON, or None when the site cannot be reached or answers badly."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None  # refused, reset, timed out, truncated, or not JSON


def load(
    *,
    fetcher: Callable[[], Any] | None = None,
    cache_path: Path | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Replace the table from the freshest source that works; return its name
    (``cache``, ``served``, ``stale cache`` or ``bundled``). ``fetcher`` and
    ``cache_path`` default to the module's ``fetch`` and ``CACHE_PATH``, read
    on each call, so tests can turn both off in one place (tests/__init__.py)."""
    global _table
    fetcher = fetcher or fetch
    cache_path = cache_path or CACHE_PATH
    try:
        fresh = now() - cache_path.stat().st_mtime < CACHE_MAX_AGE_SECONDS
    except OSError:
        fresh = False
    if fresh and (rows := _read(cache_path)):
        _table = rows
        return "cache"
    raw = fetcher()
    if rows := parse(raw):
        _table = rows
        _save_cache(cache_path, raw)
        return "served"
    if rows := _read(cache_path):
        _table = rows
        return "stale cache"
    _table = _read(BUNDLED_PATH)
    return "bundled"


def _save_cache(path: Path, raw: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    except OSError:
        return  # an unsaved cache only means the next run fetches again
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(raw, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def row(code: str | None) -> Supply | None:
    return _table.get(code) if code else None


def listed(code: str | None) -> bool:
    return row(code) is not None


def is_food(code: str | None) -> bool:
    """Heals when picked up (apple, berry, golden cap, mushroom)."""
    r = row(code)
    return r is not None and r.heal is not None and r.eaten_on_pickup


def is_potion(code: str | None) -> bool:
    """Heals when used on yourself (``use_effects`` has ``heal``)."""
    r = row(code)
    return r is not None and "heal" in r.use_effects


def heals(code: str | None) -> bool:
    return is_food(code) or is_potion(code)


def is_weapon(code: str | None) -> bool:
    r = row(code)
    return r is not None and r.supply_class == "weapon"


def kept_on_break(code: str | None) -> bool:
    """A break with it armed does not use it up (``used_up_on_break`` false)."""
    r = row(code)
    return r is not None and r.used_up_on_break is False


def break_capabilities(code: str | None) -> frozenset[str]:
    """The block-breaking capabilities among its ``use_effects`` (cut, chop, …)."""
    r = row(code)
    return r.use_effects & CAPABILITIES if r is not None else frozenset()


def block_reach(code: str | None) -> int:
    """How far a ``Use`` on a block reaches with ``code`` armed."""
    r = row(code)
    return r.attack_range if r is not None and r.attack_range is not None else DEFAULT_BLOCK_REACH


def file_capabilities(items: dict[str, dict[str, Any]]) -> None:
    """Merge every row's break capabilities onto the item table (A54), by the
    same grow-only rules as a break that opened a block (A46)."""
    for code in sorted(_table):
        caps = break_capabilities(code)
        if caps:
            absorb_supply_entry(items, {"supply_subtype_code": code, "capabilities": sorted(caps)})


def load_for_run(knowledge: KnowledgeBase, out: Callable[[str], None]) -> None:
    """Once at the start of a run: load the table and file its capabilities
    on the world's item table."""
    source = load()
    with knowledge.lock:
        file_capabilities(knowledge.items)
    out(f"supplies reference: {len(_table)} subtypes ({source})")
