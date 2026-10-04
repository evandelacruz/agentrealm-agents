"""Learned stats per ``supply_subtype_code`` (A18, M8).

Filled from play: weapon reach and damage after ``Arm``, damage taken while
an item is ``Wear`` ed, ``gem_price`` on supplies seen, and break/light/water
capabilities named on supply payloads.
"""

from __future__ import annotations

from typing import Any, Iterable

CAPABILITIES = frozenset({"cut", "chop", "smash", "burn", "blast", "light", "water"})


def _supply_code(entry: Any) -> str | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        code = entry.get("supply_subtype_code") or entry.get("code")
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


def capabilities_from_supply(entry: dict) -> list[str]:
    caps = entry.get("capabilities")
    if isinstance(caps, list):
        return sorted({c for c in caps if isinstance(c, str) and c in CAPABILITIES})
    one = entry.get("capability")
    if isinstance(one, str) and one in CAPABILITIES:
        return [one]
    return sorted(c for c in CAPABILITIES if entry.get(c))


def loadout_from_inventory(inv: dict | None) -> tuple[str | None, dict[str, str]]:
    """Armed subtype and worn slot -> subtype from a snapshot inventory."""
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


def _merge_capabilities(entry: dict[str, Any], caps: Iterable[str]) -> None:
    merged = set(entry.get("capabilities") or []) | {c for c in caps if c in CAPABILITIES}
    if merged:
        entry["capabilities"] = sorted(merged)


def merge_item(items: dict[str, dict[str, Any]], code: str | None, **facts: Any) -> None:
    """Merge observed facts into the per-world item table (in place)."""
    if not code:
        return
    row = items.setdefault(code, {})
    for key, value in facts.items():
        if value is None:
            continue
        if key == "capabilities":
            _merge_capabilities(row, value if isinstance(value, Iterable) and not isinstance(value, str) else [value])
        elif key == "attack_range":
            n = _positive_int(value)
            if n is not None:
                row["attack_range"] = n
        elif key == "weapon_damage":
            n = _positive_int(value)
            if n is not None:
                row["weapon_damage"] = max(row.get("weapon_damage", 0), n)
        elif key == "damage_taken":
            n = _positive_int(value)
            if n is not None:
                row["damage_taken"] = max(row.get("damage_taken", 0), n)
        elif key == "gem_price":
            n = _positive_int(value)
            if n is not None:
                row["gem_price"] = n


def absorb_supply_entry(items: dict[str, dict[str, Any]], entry: dict) -> None:
    code = _supply_code(entry)
    if not code:
        return
    price = _positive_int(entry.get("gem_price"))
    caps = capabilities_from_supply(entry)
    merge_item(items, code, gem_price=price, capabilities=caps or None)


def absorb_inventory(items: dict[str, dict[str, Any]], inv: dict | None) -> None:
    if not inv:
        return
    armed = inv.get("armed")
    if isinstance(armed, dict):
        absorb_supply_entry(items, armed)
    worn = inv.get("worn")
    if isinstance(worn, dict):
        for entry in worn.values():
            if isinstance(entry, dict):
                absorb_supply_entry(items, entry)
    for key in ("held", "chest"):
        bag = inv.get(key)
        if isinstance(bag, list):
            for entry in bag:
                if isinstance(entry, dict):
                    absorb_supply_entry(items, entry)


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
    if not entities:
        return
    for s in _entity_kind_entries(entities, "supplies"):
        absorb_supply_entry(items, s)
    for ch in _entity_kind_entries(entities, "chests"):
        for entry in ch.get("contents") or []:
            if isinstance(entry, dict):
                absorb_supply_entry(items, entry)


def absorb_npc_damaged(items: dict[str, dict[str, Any]], armed_code: str | None, ev: dict) -> None:
    if not armed_code or ev.get("kind") != "NPCDamaged":
        return
    amount = _positive_int(ev.get("amount"))
    if amount is not None:
        merge_item(items, armed_code, weapon_damage=amount)


def absorb_damaged_while_worn(
    items: dict[str, dict[str, Any]], worn_codes: dict[str, str], ev: dict
) -> None:
    if ev.get("kind") != "Damaged":
        return
    amount = _positive_int(ev.get("amount"))
    if amount is None or not worn_codes:
        return
    for code in worn_codes.values():
        merge_item(items, code, damage_taken=amount)


def absorb_rejection_attack_range(
    items: dict[str, dict[str, Any]], armed_code: str | None, result: dict
) -> None:
    if not armed_code or result.get("outcome") == "rejected":
        rej = result.get("rejection") or {}
        if rej.get("code") == "target_out_of_range":
            merge_item(items, armed_code, attack_range=_positive_int(rej.get("attack_range")))


def absorb_self_attack_range(
    items: dict[str, dict[str, Any]], armed_code: str | None, attack_range: int | None
) -> None:
    if armed_code and attack_range is not None:
        merge_item(items, armed_code, attack_range=attack_range)
