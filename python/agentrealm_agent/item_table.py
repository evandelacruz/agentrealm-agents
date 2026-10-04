"""Learned stats per ``supply_subtype_code`` (A18, M8).

A row holds only facts the API serves for that subtype (PLAN.md A18):

- ``attack_range``: from a ``Use`` rejected ``target_out_of_range``, which
  carries the reach the sim judged by (API Use, B100). Filed under the
  subtype armed in the same response's observation. Overwritten.
- ``gem_price``: from supplies on entity reads and snapshot entities (API
  Reads, Snapshots). Overwritten, since prices are tuned in play.
- ``weapon_damage``: ``{npc_type_code: max hit}``, the largest ``NPCDamaged``
  amount seen against that NPC type, from the one ``NPCDamaged`` on the block
  and tick an applied ``Use`` resolved, when no other character is in sight,
  filed under the subtype armed after that response's observation (API
  Events). Damage is rolled and the target's defense lowers it (docs/
  GAME_NOTES.md Combat), so one hit is a sample, not the weapon's stat; the
  max is kept per NPC type and only ever rises.

Damage taken per worn item and capabilities are not stored: see PLAN.md A18.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .threat import damage_amount

Pos = tuple[int, int]

# A new character's carried chest holds 10 (Manual §11, "a new, empty blue
# chest (10)"). The snapshot serves no capacity field, and how a bigger chest
# changes it is unknown (docs/GAME_NOTES.md open questions).
DEFAULT_CARRY_CAPACITY = 10


@dataclass(frozen=True)
class InventorySupply:
    """A supply's ``id`` and ``supply_subtype_code``, as the snapshot's
    ``inventory`` and a ground chest's ``contents`` list it (API Snapshots)."""

    id: int
    code: str = ""


@dataclass(frozen=True)
class AppliedUse:
    """An applied ``Use`` intent, for matching block-anchored combat events.

    ``npc_type`` is the NPC we saw on the target block when the ``Use``
    applied, or empty when there was not exactly one.
    """

    tick: int
    map_id: int | None
    x: int
    y: int
    npc_type: str = ""


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


def supplies_from_list(raw: Any) -> list[InventorySupply]:
    """``[{id, supply_subtype_code}, …]`` as supplies; malformed entries are skipped."""
    if not isinstance(raw, list):
        return []
    out: list[InventorySupply] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sid = _positive_int(entry.get("id"))
        if sid is None:
            continue
        out.append(InventorySupply(sid, _supply_code(entry) or ""))
    return out


def carried_from_inventory(
    inv: dict | None,
) -> tuple[list[InventorySupply], list[InventorySupply], str | None, dict[str, str]]:
    """Held, stowed in the carried chest, armed code, and worn slot -> code.

    The snapshot's ``inventory`` is ``gems``, ``armed``, ``worn`` by slot,
    ``held``, and ``chest`` (API Snapshots).
    """
    if not inv:
        return [], [], None, {}
    armed = _supply_code(inv.get("armed"))
    worn: dict[str, str] = {}
    raw = inv.get("worn")
    if isinstance(raw, dict):
        for slot, entry in raw.items():
            code = _supply_code(entry)
            if code:
                worn[str(slot)] = code
    return supplies_from_list(inv.get("held")), supplies_from_list(inv.get("chest")), armed, worn


def loadout_from_inventory(inv: dict | None) -> tuple[str | None, dict[str, str]]:
    """Armed subtype and worn slot -> subtype from a snapshot ``inventory``.

    Each supply there is an ``id`` and ``supply_subtype_code``; ``worn`` is
    keyed by slot (API Snapshots).
    """
    _, _, armed, worn = carried_from_inventory(inv)
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


def merge_weapon_hit(items: dict[str, dict[str, Any]], code: str | None, npc_type: str, amount: Any) -> None:
    """Raise ``weapon_damage[npc_type]`` under ``code`` to ``amount`` if larger.

    A missing or invalid code, NPC type, or amount is ignored.
    """
    n = _positive_int(amount)
    if not code or not npc_type or n is None:
        return
    row = items.setdefault(code, {})
    per_type = row.get("weapon_damage")
    if not isinstance(per_type, dict):
        per_type = row["weapon_damage"] = {}
    old = _positive_int(per_type.get(npc_type))
    if old is None or n > old:
        per_type[npc_type] = n


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


def use_target_block(intent: dict, entities: list[Any]) -> Pos | None:
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


def npc_type_on_block(block: Pos, entities: list[Any]) -> str:
    """``npc_type_code`` of the one NPC seen on ``block``, else empty."""
    codes = [e.code for e in entities if e.kind == "npc" and e.pos == block]
    return codes[0] if len(codes) == 1 and codes[0] else ""


def _event_place(ev: dict, default_map_id: int | None) -> tuple[int, int | None, int, int] | None:
    """``(tick, map_id, x, y)`` of a block-anchored event, or None when malformed."""
    try:
        tick, x, y = int(ev["tick"]), int(ev["x"]), int(ev["y"])
        raw_map = ev.get("map_id")
        mid = int(raw_map) if raw_map is not None else default_map_id
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return tick, mid, x, y


def absorb_npc_damaged(
    items: dict[str, dict[str, Any]],
    events: list[dict],
    applied_uses: list[AppliedUse],
    *,
    default_map_id: int | None,
    armed_code: str | None,
    others_in_sight: bool,
) -> None:
    """Record the hit from the one ``NPCDamaged`` our ``Use`` can own.

    ``NPCDamaged`` is copied to every character that sees the block (API
    Events) and a miss emits nothing, so a hit is ours only when no other
    character could have struck: none in sight, and exactly one ``NPCDamaged``
    on the block and tick our ``Use`` resolved (docs/GAME_NOTES.md). It is
    kept as the max per NPC type seen on that block (``merge_weapon_hit``); a
    ``Use`` with no single NPC type on its block records nothing.
    """
    if not armed_code or not applied_uses or others_in_sight:
        return
    hits: dict[AppliedUse, list[int | None]] = {u: [] for u in applied_uses if u.npc_type}
    for ev in events:
        if ev.get("kind") != "NPCDamaged":
            continue
        place = _event_place(ev, default_map_id)
        if place is None:
            continue
        tick, mid, x, y = place
        for u in hits:
            if (u.tick, u.map_id, u.x, u.y) == (tick, mid, x, y):
                hits[u].append(damage_amount(ev))
    for u, amounts in hits.items():
        if len(amounts) == 1:
            merge_weapon_hit(items, armed_code, u.npc_type, amounts[0])
