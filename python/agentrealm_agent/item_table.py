"""Learned stats per ``supply_subtype_code`` (A18, M8).

A row holds only facts the API serves for that subtype (PLAN.md A18):

- ``attack_range``: from a ``Use`` rejected ``target_out_of_range``, which
  carries the reach the sim judged by (API Use, B100). Filed under the
  subtype armed in the same response's observation. Overwritten.
- ``gem_price``: from supplies on entity reads and snapshot entities (API
  Reads, Snapshots). Overwritten, since prices are tuned in play.

Weapon damage, damage taken while worn, and capabilities are not stored yet:
see PLAN.md A18 for why.
"""

from __future__ import annotations

from typing import Any, Iterable


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
    for key in ("attack_range", "gem_price"):
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
