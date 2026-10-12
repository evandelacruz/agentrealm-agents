"""The Manual's Supplies reference: one row per supply subtype (A54).

The site serves the table as JSON at ``SUPPLIES_URL`` (API Supplies
reference, saims B132): per ``code`` its ``class``, ``slot``,
``capabilities``, ``used_up_on_break``, ``eaten_on_pickup``, ``gem_prices``
and more; a field that does not apply is left out. Each capability names
what the supply does (``attacks``, ``cuts``, ``heals``, ``holds``,
``protects``, …), when (its ``trigger``: ``used``, ``equipped``, ``worn`` or
``picked_up``), and its numbers (``amount``, ``reach``, ``cooldown_ms``,
``duration_seconds``) where it takes them (saims B138). ``parse`` reads
those into the fields of ``Supply`` (A123): a weapon's ``damage``, attack
range and cooldown from ``attacks``, a potion's or food's ``heal`` from
``heals``, a chest's ``chest_capacity`` from ``holds``, an armor piece's
``defense`` from ``protects``. Before B138 each row carried ``use_effects``
and a field per stat (``damage``, ``heal``, …) instead; ``parse`` still reads
that shape, so a cache saved before it works the same. It is a page on the
website, not an API read, so it spends nothing from a character's call
budget, and it is fetched once per run (``load``).

Where the table comes from, first that works:

1. ``CACHE_PATH``, when it is younger than ``CACHE_MAX_AGE_SECONDS`` (the site
   serves it with ``max-age=3600``) and in the current row shape (every row
   carries ``capabilities``, ``current_shape``), so back-to-back runs fetch it
   once. A cache in the shape before B138 was saved before the game's table
   changed, so it is stale whatever its age (A126).
2. ``SUPPLIES_URL``, saved to ``CACHE_PATH`` on success.
3. ``CACHE_PATH`` at any age or shape, when the site cannot be reached.
4. ``BUNDLED_PATH``, a copy checked in with the agent. The module starts on it,
   so tests and a run that never calls ``load`` still have every row.

A fallback (3 or 4) names the row shape it used, so the run log shows a table
read in the older shape.

The questions the agent asks of a subtype (is it food, a potion, a weapon;
what blocks it breaks; how much damage it deals) are the functions below. A
code the table does not list answers "no" (or None): learning in play (A18,
A28, A46) still covers it.
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
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .config import STATE_DIR
from .item_table import DEFAULT_CARRY_CAPACITY, absorb_supply_entry

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
# The ``use_effects`` name of each served capability that acts while the
# supply is armed or used (A123). ``use_effects`` is the agent's word for what
# armed gear does: a capability that acts only worn (armor's ``protects``) or
# on pickup (food's ``heals``), or one no rule here reads (``detects``,
# ``speeds``, …), names none.
USE_EFFECT_OF = {
    "attacks": "attack",
    "cuts": "cut",
    "chops": "chop",
    "smashes": "smash",
    "burns": "burn",
    "blasts": "blast",
    "heals": "heal",
    "holds": "chest",
    "lights": "light",
    "teleports": "teleport",
    "traps": "trap",
    "fords": "water",
}
ARMED_TRIGGERS = frozenset({"used", "equipped"})
# A weapon's attack cooldown when its row sets none (the Manual's rules: 1 s).
DEFAULT_ATTACK_COOLDOWN_MS = 1000
# The classes that go in a worn slot; every other class is armed, used or carried.
WORN_CLASSES = frozenset({"armor", "accessory"})
# The ``use_effects`` a ``Use`` on yourself applies to the holder (A108): a
# potion's ``heal`` and a chest's ``chest`` (a bigger carried chest; saims
# docs Gameplay.md supply classes: "A chest upgrade is a consumable too").
# A new effect of that kind (a timed one, say) is added here, and the one
# Use-self path (``states/use_item.py``) applies it.
SELF_EFFECTS = frozenset({"heal", "chest"})


@dataclass(frozen=True)
class Supply:
    """One row of the Supplies reference, the fields the agent reads."""

    code: str
    supply_class: str
    use_effects: frozenset[str]
    slot: str = ""
    damage: int | None = None
    attack_range: int | None = None
    attack_cooldown_ms: int | None = None  # served since B138; None in the older shape
    defense: int | None = None
    heal: int | None = None
    chest_capacity: int | None = None
    used_up_on_break: bool | None = None
    eaten_on_pickup: bool = False
    fords: bool = False  # water is walkable while it is armed or worn (``fords``, A133)
    sold: bool = False  # a shop sells it in some world: ``gem_prices`` names one


def parse(raw: Any) -> dict[str, Supply]:
    """Rows by code from the served JSON array; malformed rows are skipped.
    A row with a ``capabilities`` list is read by it (saims B138), any other
    by its ``use_effects`` and stat fields, the shape before it."""
    if not isinstance(raw, list):
        return {}
    out: dict[str, Supply] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        if not isinstance(code, str) or not code:
            continue
        supply_class = row.get("class") if isinstance(row.get("class"), str) else ""
        caps = row.get("capabilities")
        stats = _capability_stats(caps, supply_class) if isinstance(caps, list) else _stat_fields(row)
        used_up = row.get("used_up_on_break")
        out[code] = Supply(
            code=code,
            supply_class=supply_class,
            slot=row.get("slot") if isinstance(row.get("slot"), str) else "",
            used_up_on_break=used_up if isinstance(used_up, bool) else None,
            eaten_on_pickup=row.get("eaten_on_pickup") is True,
            sold=_sold(row.get("gem_prices")),
            **stats,
        )
    return out


def _capability_stats(caps: list[Any], supply_class: str) -> dict[str, Any]:
    """The ``Supply`` fields a row's ``capabilities`` give (A123)."""
    by_name: dict[str, dict[str, Any]] = {}
    effects: set[str] = set()
    for cap in caps:
        if not isinstance(cap, dict) or not isinstance(cap.get("capability"), str):
            continue
        name = cap["capability"]
        by_name.setdefault(name, cap)
        if name in USE_EFFECT_OF and cap.get("trigger") in ARMED_TRIGGERS:
            effects.add(USE_EFFECT_OF[name])
    attacks = by_name.get("attacks", {})
    defense = _non_negative_int(by_name.get("protects", {}).get("amount"))
    if defense is None and supply_class == "armor":
        defense = 0  # armor without ``protects`` adds none
    return {
        "use_effects": frozenset(effects),
        "damage": _non_negative_int(attacks.get("amount")),
        "attack_range": _positive_int(attacks.get("reach")),
        "attack_cooldown_ms": _positive_int(attacks.get("cooldown_ms")),
        "defense": defense,
        "heal": _positive_int(by_name.get("heals", {}).get("amount")),
        "chest_capacity": _positive_int(by_name.get("holds", {}).get("amount")),
        "fords": "fords" in by_name,
    }


def _stat_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The ``Supply`` fields a row in the shape before B138 gives."""
    effects = row.get("use_effects")
    return {
        "use_effects": frozenset(e for e in effects if isinstance(e, str)) if isinstance(effects, list) else frozenset(),
        "damage": _non_negative_int(row.get("damage")),
        "attack_range": _positive_int(row.get("attack_range")),
        "defense": _non_negative_int(row.get("defense")),
        "heal": _positive_int(row.get("heal")),
        "chest_capacity": _positive_int(row.get("chest_capacity")),
        "fords": isinstance(effects, list) and "water" in effects,
    }


def _sold(prices: Any) -> bool:
    """Whether ``gem_prices`` gives a price in at least one world."""
    return isinstance(prices, dict) and any(_positive_int(v) is not None for v in prices.values())


def _non_negative_int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v >= 0 else None


def _positive_int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v > 0 else None


def current_shape(raw: Any) -> bool:
    """Whether the served JSON is in the row shape since saims B138: a
    non-empty array whose every row carries a ``capabilities`` list."""
    rows = [r for r in raw if isinstance(r, dict) and r.get("code")] if isinstance(raw, list) else []
    return bool(rows) and all(isinstance(r.get("capabilities"), list) for r in rows)


def _shape_name(raw: Any) -> str:
    return "capabilities shape" if current_shape(raw) else "shape before B138"


def _read_raw(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read(path: Path) -> dict[str, Supply]:
    return parse(_read_raw(path))


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
    (``cache``, ``served``, or ``stale cache`` or ``bundled`` with the row
    shape it was in). ``fetcher`` and ``cache_path`` default to the module's
    ``fetch`` and ``CACHE_PATH``, read on each call, so tests can turn both off
    in one place (tests/__init__.py)."""
    global _table
    fetcher = fetcher or fetch
    cache_path = cache_path or CACHE_PATH
    try:
        young = now() - cache_path.stat().st_mtime < CACHE_MAX_AGE_SECONDS
    except OSError:
        young = False
    cached = _read_raw(cache_path)
    if young and current_shape(cached) and (rows := parse(cached)):
        _table = rows
        return "cache"
    raw = fetcher()
    if rows := parse(raw):
        _table = rows
        _save_cache(cache_path, raw)
        return "served"
    if rows := parse(cached):
        _table = rows
        return f"stale cache, {_shape_name(cached)}"
    bundled = _read_raw(BUNDLED_PATH)
    _table = parse(bundled)
    return f"bundled, {_shape_name(bundled)}"


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


def eaten_on_pickup(code: str | None) -> bool:
    """Used up the moment it is taken (food, gems, a heart): its effect applies
    at once and nothing is left in the pack (``eaten_on_pickup``)."""
    r = row(code)
    return r is not None and r.eaten_on_pickup


def heals(code: str | None) -> bool:
    return is_food(code) or is_potion(code)


def is_weapon(code: str | None) -> bool:
    r = row(code)
    return r is not None and r.supply_class == "weapon"


def fords(code: str | None) -> bool:
    """Water is walkable while it is armed or worn (a raft; Manual Supplies
    capability table, ``fords``)."""
    r = row(code)
    return r is not None and r.fords


def kept_on_break(code: str | None) -> bool:
    """A break with it armed does not use it up (``used_up_on_break`` false)."""
    r = row(code)
    return r is not None and r.used_up_on_break is False


def break_capabilities(code: str | None) -> frozenset[str]:
    """The block-breaking capabilities among its ``use_effects`` (cut, chop, …)."""
    r = row(code)
    return r.use_effects & CAPABILITIES if r is not None else frozenset()


def weapon_damage(code: str | None) -> int | None:
    """A weapon's published ``damage`` (0 for the bare mallet and whip), or
    None for anything that is not a listed weapon."""
    r = row(code)
    return r.damage if r is not None and r.supply_class == "weapon" else None


def weapon_cooldown_ms(code: str | None) -> int | None:
    """A weapon's attack cooldown: the least time between two of its swings
    (``attacks`` ``cooldown_ms``; ``DEFAULT_ATTACK_COOLDOWN_MS`` when the row
    sets none), or None for anything that is not a listed weapon."""
    r = row(code)
    if r is None or r.supply_class != "weapon":
        return None
    return r.attack_cooldown_ms or DEFAULT_ATTACK_COOLDOWN_MS


def attack_reach(code: str | None) -> int | None:
    """A weapon's published attack range (``attacks`` ``reach``), or None for
    anything that is not a listed weapon or a row that gives none."""
    r = row(code)
    return r.attack_range if r is not None and r.supply_class == "weapon" else None


def armor_defense(code: str | None) -> int | None:
    """An armor piece's published ``defense``, or None for anything that is not listed armor."""
    r = row(code)
    return r.defense if r is not None and r.supply_class == "armor" else None


def worn_armor_defense(worn_codes: Iterable[str]) -> int:
    """The defense the armor among ``worn_codes`` adds: each listed armor
    piece's ``defense``. Only worn armor protects; armor in hand adds none, so
    callers pass the worn slots' codes, never the armed one."""
    return sum(armor_defense(code) or 0 for code in worn_codes)


def sold_codes() -> list[str]:
    """Every subtype a shop sells in some world (``gem_prices``), sorted: what
    a buy can ever get. Gems, lives, the starting kit, worn-slot rows and
    anything else never priced are left out (A110)."""
    return sorted(code for code, r in _table.items() if r.sold)


def used_on_self(code: str | None) -> bool:
    """A consumable whose ``Use`` on yourself applies its effect to the holder
    (``SELF_EFFECTS``): a potion, a chest. Held, it does nothing until used."""
    r = row(code)
    return r is not None and r.supply_class == "consumable" and bool(r.use_effects & SELF_EFFECTS)


def chest_capacity(code: str | None) -> int | None:
    """The carry capacity a listed chest gives the carried chest when that is
    larger (the blue chest, 10, is the size a character starts with), or None
    for anything that is not a chest. A ``Use`` of it on yourself raises the
    pack (``loot.learn_chest_upgrade``, A108); holding it does not."""
    r = row(code)
    return r.chest_capacity if r is not None else None


def chest_applied(code: str | None, carry_capacity: int) -> bool:
    """Whether ``code`` is a chest that raised the pack to its carry slots
    (A106): larger than the chest a character starts with, and the pack
    holds at least that many now."""
    slots = chest_capacity(code)
    return slots is not None and slots > DEFAULT_CARRY_CAPACITY and carry_capacity >= slots


def chest_upgrade(code: str | None, carry_capacity: int) -> bool:
    """Whether using ``code`` on yourself would give the pack more slots than
    ``carry_capacity``: a bigger chest, always worth applying (A108)."""
    slots = chest_capacity(code)
    return slots is not None and slots > carry_capacity


def worn(code: str | None) -> bool | None:
    """Whether the reference says a subtype is worn: True for ``armor`` and
    ``accessory``, False for every other class it lists (weapons, tools,
    consumables, gems: armed, used or carried, never worn), None when the
    reference does not list it."""
    r = row(code)
    return None if r is None else r.supply_class in WORN_CLASSES


def worn_slot(code: str | None) -> str:
    """The slot listed armor or an accessory is worn in (``body``, ``accessory``, …), or ``""``."""
    r = row(code)
    return r.slot if r is not None and r.supply_class in WORN_CLASSES else ""


def what_it_does(code: str | None) -> dict[str, Any]:
    """What the Supplies reference says a subtype does, for the planner's State
    (A92): ``class``, ``use`` (its ``use_effects``), whichever of ``heal``,
    ``damage``, ``defense``, ``chest_capacity`` and ``slot`` apply (``defense``
    counts only while worn), a weapon's ``cooldown_ms`` between swings (A123),
    ``used_up_on_break`` when
    listed (true: each block broken with it uses one up), ``eaten_on_pickup``
    when it is used up on its ``Take`` (A112), and ``fords`` when water is
    walkable while it is armed or worn (A133). ``{}`` when unlisted."""
    r = row(code)
    if r is None:
        return {}
    out: dict[str, Any] = {"class": r.supply_class, "use": sorted(r.use_effects)}
    for key in ("heal", "damage", "defense", "chest_capacity"):
        if getattr(r, key) is not None:
            out[key] = getattr(r, key)
    if (cooldown := weapon_cooldown_ms(code)) is not None:
        out["cooldown_ms"] = cooldown
    if r.slot and r.slot != "armed":
        out["slot"] = r.slot
    if r.used_up_on_break is not None:
        out["used_up_on_break"] = r.used_up_on_break
    if r.eaten_on_pickup:
        out["eaten_on_pickup"] = True
    if r.fords:
        out["fords"] = True
    return out


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
