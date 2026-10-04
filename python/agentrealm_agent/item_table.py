"""Learned stats per ``supply_subtype_code`` (A18, M8).

A row holds only facts the API serves for that subtype (PLAN.md A18):

- ``attack_range``: from a ``Use`` rejected ``target_out_of_range``, which
  carries the reach the sim judged by (API Use, B100). Filed under the
  subtype armed in the same response's observation. Overwritten.
- ``gem_price``: from supplies on entity reads and snapshot entities (API
  Reads, Snapshots). Overwritten, since prices are tuned in play.
- ``weapon_damage``: from ``NPCDamaged`` on the same block and tick as an
  applied ``Use``, filed under the subtype armed after that response's
  observation (API Events).
- ``damage_taken``: from a ``Damaged`` event when exactly one worn slot is
  filled, filed under that subtype (one hit cannot be split across slots).

Capabilities are not stored yet: see PLAN.md A18 and Server gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .threat import damage_amount

Pos = tuple[int, int]


@dataclass(frozen=True)
class HeldSupply:
    supply_id: int
    code: str


@dataclass(frozen=True)
class AppliedUse:
    """An applied ``Use`` intent, for matching block-anchored combat events."""

    tick: int
    map_id: int | None
    x: int
    y: int


def _supply_code(entry: Any) -> str | None:
    if isinstance(entry, dict):
        code = entry.get("supply_subtype_code")
        if isinstance(code, str) and code:
            return code
    return None


def _positive_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n > 0 else None


def held_from_inventory(inv: dict | None) -> list[HeldSupply]:
    if not inv:
        return []
    raw = inv.get("held")
    if not isinstance(raw, list):
        return []
    out: list[HeldSupply] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = _supply_code(entry)
        if not code:
            continue
        try:
            sid = int(entry["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(HeldSupply(sid, code))
    return out


def loadout_from_inventory(inv: dict | None) -> tuple[str | None, dict[str, str]]:
    """Armed subtype and worn slot -> subtype from a snapshot ``inventory``.

    Each supply there is an ``id`` and ``supply_subtype_code``; ``worn`` is
    keyed by slot (API Snapshots).
    """
    if not inv:
        return None, {}
    armed = _supply_code(inv.get("armed"))
    worn: dict[str, str] = {}
    raw = inv.get("worn")
    if isinstance(raw, dict):
        for slot, entry in raw.items():
            code = _supply_code(entry)
            if code:
                worn[str(slot)] = code
    return armed, worn


def merge_item(items: dict[str, dict[str, Any]], code: str | None, **facts: Any) -> None:
    """Merge observed facts into the per-world item table (in place).

    Invalid or missing values are ignored, and a row is created only when at
    least one fact is kept.
    """
    if not code:
        return
    kept: dict[str, int] = {}
    for key in ("attack_range", "gem_price", "weapon_damage", "damage_taken"):
        n = _positive_int(facts.get(key))
        if n is not None:
            kept[key] = n
    if kept:
        items.setdefault(code, {}).update(kept)


def absorb_supply_entry(items: dict[str, dict[str, Any]], entry: dict) -> None:
    merge_item(items, _supply_code(entry), gem_price=entry.get("gem_price"))


def _entity_kind_entries(entities: dict, kind: str) -> Iterable[dict]:
    raw = entities.get(kind)
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                yield entry
    elif isinstance(raw, dict):
        for key in ("added", "changed"):
            for entry in raw.get(key) or []:
                if isinstance(entry, dict):
                    yield entry


def absorb_entities_payload(items: dict[str, dict[str, Any]], entities: dict | None) -> None:
    """Prices from supplies in an entity read, a snapshot, or a delta patch."""
    if not entities:
        return
    for s in _entity_kind_entries(entities, "supplies"):
        absorb_supply_entry(items, s)


def rejection_attack_range(result: dict) -> int | None:
    """Reach carried by a ``target_out_of_range`` rejection, else None."""
    if result.get("outcome") != "rejected":
        return None
    rej = result.get("rejection") or {}
    if rej.get("code") != "target_out_of_range":
        return None
    return _positive_int(rej.get("attack_range"))


def absorb_attack_range(
    items: dict[str, dict[str, Any]], armed_code: str | None, attack_range: int | None
) -> None:
    if armed_code and attack_range is not None:
        merge_item(items, armed_code, attack_range=attack_range)


def use_target_block(
    intent: dict,
    *,
    map_id: int | None,
    self_pos: Pos | None,
    entities: list[Any],
) -> Pos | None:
    """Block a ``Use`` resolves against, or None when it cannot be named."""
    target = intent.get("target")
    if not isinstance(target, dict):
        return None
    kind = target.get("kind")
    if kind == "block":
        try:
            return (int(target["x"]), int(target["y"]))
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "character":
        try:
            cid = int(target["character_id"])
        except (KeyError, TypeError, ValueError):
            return None
        for e in entities:
            if e.kind == "character" and e.id == cid:
                return e.pos
        return None
    return None


def _event_block(ev: dict, default_map_id: int | None) -> tuple[int | None, int, int] | None:
    try:
        x, y = int(ev["x"]), int(ev["y"])
    except (KeyError, TypeError, ValueError):
        return None
    raw_map = ev.get("map_id", default_map_id)
    try:
        mid = int(raw_map) if raw_map is not None else None
    except (TypeError, ValueError):
        mid = default_map_id
    return mid, x, y


def _same_block(
    use: AppliedUse, tick: int, map_id: int | None, x: int, y: int, default_map_id: int | None
) -> bool:
    if use.tick != tick:
        return False
    if use.x != x or use.y != y:
        return False
    use_map = use.map_id if use.map_id is not None else default_map_id
    ev_map = map_id if map_id is not None else default_map_id
    return use_map == ev_map


def absorb_npc_damaged(
    items: dict[str, dict[str, Any]],
    events: list[dict],
    applied_uses: list[AppliedUse],
    *,
    default_map_id: int | None,
    armed_code: str | None,
) -> None:
    """Record weapon damage when our ``Use`` and ``NPCDamaged`` share block and tick."""
    if not armed_code or not applied_uses:
        return
    for ev in events:
        if ev.get("kind") != "NPCDamaged":
            continue
        amount = damage_amount(ev)
        if amount is None:
            continue
        block = _event_block(ev, default_map_id)
        if block is None:
            continue
        mid, x, y = block
        tick = int(ev.get("tick", 0))
        if not any(_same_block(u, tick, mid, x, y, default_map_id) for u in applied_uses):
            continue
        merge_item(items, armed_code, weapon_damage=amount)


def absorb_damaged_worn(
    items: dict[str, dict[str, Any]], events: list[dict], worn_codes: dict[str, str]
) -> None:
    """Record damage taken on the sole worn item when a hit cannot be split."""
    if len(worn_codes) != 1:
        return
    code = next(iter(worn_codes.values()))
    for ev in events:
        if ev.get("kind") != "Damaged":
            continue
        amount = damage_amount(ev)
        if amount is None:
            continue
        merge_item(items, code, damage_taken=amount)
