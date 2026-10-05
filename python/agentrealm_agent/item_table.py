"""Learned stats per ``supply_subtype_code`` (A18, M8).

A row holds only facts the API serves for that subtype (PLAN.md A18):

- ``attack_range``: from a ``Use`` rejected ``target_out_of_range``, which
  carries the reach the sim judged by (API Use, B100). Filed under the
  subtype armed in the same response's observation. Overwritten.
- ``gem_price``: from supplies on entity reads and snapshot entities (API
  Reads, Snapshots). Overwritten, since prices are tuned in play.
- ``weapon_damage``: ``{npc_type_code: max hit}``, the largest ``NPCDamaged``
  amount seen against that NPC type, from the one ``NPCDamaged`` on the block
  and tick an applied ``Use`` resolved, when no other character was in sight
  when the ``Use`` applied or at the response's observation, filed under the subtype armed after that response's observation (API
  Events). Damage is rolled and the target's defense lowers it (docs/
  GAME_NOTES.md Combat), so one hit is a sample, not the weapon's stat; the
  max is kept per NPC type and only ever rises.
- ``damage_taken``: ``{npc_type_code: max hit}``, the largest ``Damaged`` amount
  from that NPC type while this subtype was the only filled worn slot for the
  whole response (API Events, Snapshots). Rolled damage depends on the
  attacker's power and our defense (docs/GAME_NOTES.md Combat), so it is a
  sample, not the item's defense stat.
- ``damage_without``: ``{npc_type_code: max hit}`` from that NPC type while no
  worn slot was filled, filed only under the subtype that was worn alone just
  before the slots emptied.
- ``damage_saved``: ``{npc_type_code: damage_without − damage_taken}``,
  recomputed from the row's two maxes whenever either changes, and present
  only while both exist and the gap is positive. It estimates how far the item
  lowers that type's best hit, not the item's defense stat.

- ``capabilities``: sorted list of the capabilities (``cut``, ``chop``,
  ``smash``, ``burn``, ``blast``) this subtype has opened a block with: the
  ``BlockChanged`` a break waited for, with this subtype armed after that
  response's observation (A28, A46). Only ever grows. A failed break proves
  nothing about the item (the block decides, docs/GAME_NOTES.md Breaking
  blocks), so it removes nothing. No read serves capabilities (PLAN.md Server
  gaps), and the manual's per-class rules are not stored here: they are
  applied at read time in ``break_memory.MANUAL_CAPABILITIES``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .threat import damage_amount, type_key_from_damaged

Pos = tuple[int, int]

# A new character's carried chest holds 10 (Manual §11, "a new, empty blue
# chest (10)"). The snapshot serves no capacity field, and how a bigger chest
# changes it is unknown (docs/GAME_NOTES.md open questions).
DEFAULT_CARRY_CAPACITY = 10


class FragmentMeta:
    """``fragment`` metadata on a held fragment supply (API Snapshots).

    ``missing_slots`` is None when the server's list could not be read; the
    fragment is still kept, and completeness falls back to the slots held.
    """

    __slots__ = ("composes_into", "piece_count", "slot", "missing_slots")

    def __init__(
        self,
        *,
        composes_into: str,
        piece_count: int,
        slot: int,
        missing_slots: tuple[int, ...] | None,
    ) -> None:
        self.composes_into = composes_into
        self.piece_count = piece_count
        self.slot = slot
        self.missing_slots = missing_slots


def parse_fragment(raw: Any) -> FragmentMeta | None:
    """The ``fragment`` field, or None when it is absent or names no whole.

    Slot numbers may be 0: the docs do not say slots are 1-based.
    """
    if not isinstance(raw, dict):
        return None
    into = raw.get("composes_into")
    if not isinstance(into, str) or not into:
        return None
    piece_count = _fragment_int(raw.get("piece_count"))
    slot = _fragment_int(raw.get("slot"))
    if piece_count is None or piece_count < 1 or slot is None:
        return None
    return FragmentMeta(
        composes_into=into,
        piece_count=piece_count,
        slot=slot,
        missing_slots=_missing_slots(raw.get("missing_slots")),
    )


def _missing_slots(raw: Any) -> tuple[int, ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    slots = [_fragment_int(item) for item in raw]
    if any(n is None for n in slots):
        return None
    return tuple(n for n in slots if n is not None)


def _fragment_int(value: Any) -> int | None:
    """A non-negative int; bools and non-integral values are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


@dataclass(frozen=True)
class InventorySupply:
    """A supply's ``id`` and ``supply_subtype_code``, as the snapshot's
    ``inventory`` and a ground chest's ``contents`` list it (API Snapshots)."""

    id: int
    code: str = ""
    fragment: FragmentMeta | None = None


@dataclass(frozen=True)
class AppliedUse:
    """An applied ``Use`` intent, for matching block-anchored combat events.

    ``npc_type`` is the NPC we saw on the target block when the ``Use``
    applied, or empty when there was not exactly one. ``others_in_sight`` is
    whether another character was in sight then: one who has left by the
    response's observation could still have struck the block.
    """

    tick: int
    map_id: int | None
    x: int
    y: int
    npc_type: str = ""
    others_in_sight: bool = False


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
        out.append(InventorySupply(sid, _supply_code(entry) or "", parse_fragment(entry.get("fragment"))))
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
    for key in ("attack_range", "gem_price", "heal_amount"):
        n = _positive_int(facts.get(key))
        if n is not None:
            kept[key] = n
    on_pickup = facts.get("heal_on_pickup")
    if on_pickup is True or on_pickup is False:
        kept["heal_on_pickup"] = on_pickup
    if kept:
        items.setdefault(code, {}).update(kept)


def merge_heal(
    items: dict[str, dict[str, Any]],
    code: str | None,
    amount: Any,
    *,
    on_pickup: bool | None = None,
) -> None:
    """File heal observed from a ``Take`` or self-``Use`` (A24).

    ``heal_amount`` keeps the largest positive heal seen. ``heal_on_pickup`` is
    set when pickup heal is measured; a ``Take`` that heals nothing while hurt
    files ``False``.
    """
    n = _positive_int(amount)
    if code and n is not None and n > 0:
        row = items.setdefault(code, {})
        old = _positive_int(row.get("heal_amount"))
        if old is None or n > old:
            row["heal_amount"] = n
    if not code or on_pickup is None:
        return
    row = items.setdefault(code, {})
    if on_pickup is True:
        row["heal_on_pickup"] = True
    elif on_pickup is False and row.get("heal_on_pickup") is not True:
        row["heal_on_pickup"] = False


def merge_capability(items: dict[str, dict[str, Any]], code: str | None, capability: str) -> None:
    """File ``capability`` under ``code`` after a break with it armed opened a block (A46)."""
    if not code or not capability:
        return
    row = items.setdefault(code, {})
    have = row.get("capabilities")
    caps = {c for c in have if isinstance(c, str)} if isinstance(have, list) else set()
    if capability not in caps:
        row["capabilities"] = sorted(caps | {capability})


def _merge_max_per_npc(row: dict[str, Any], field: str, npc_type: str, amount: int) -> None:
    per_type = row.get(field)
    if not isinstance(per_type, dict):
        per_type = row[field] = {}
    old = _positive_int(per_type.get(npc_type))
    if old is None or amount > old:
        per_type[npc_type] = amount


def _refresh_damage_saved(row: dict[str, Any], npc_type: str) -> None:
    """Set ``damage_saved[npc_type]`` to ``damage_without − damage_taken`` now.

    Recomputed from the row's two maxes whenever either changes, so it never
    goes stale; dropped while either side is missing or the gap is not positive.
    """
    taken = row.get("damage_taken")
    without = row.get("damage_without")
    t = _positive_int(taken.get(npc_type)) if isinstance(taken, dict) else None
    w = _positive_int(without.get(npc_type)) if isinstance(without, dict) else None
    per_type = row.get("damage_saved")
    if t is not None and w is not None and w > t:
        if not isinstance(per_type, dict):
            per_type = row["damage_saved"] = {}
        per_type[npc_type] = w - t
    elif isinstance(per_type, dict):
        per_type.pop(npc_type, None)
        if not per_type:
            del row["damage_saved"]


def merge_worn_hit(items: dict[str, dict[str, Any]], code: str | None, field: str, npc_type: str, amount: Any) -> None:
    """Raise ``field`` (``damage_taken`` or ``damage_without``) for ``npc_type``
    under ``code``, then recompute that type's ``damage_saved``."""
    n = _positive_int(amount)
    if not code or not npc_type or n is None:
        return
    row = items.setdefault(code, {})
    _merge_max_per_npc(row, field, npc_type, n)
    _refresh_damage_saved(row, npc_type)


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
    if kind == "npc":
        # Where we last saw it: the server swings at the block it stands on that
        # tick, so a move since our read can leave the hit unmatched.
        try:
            nid = int(target["npc_id"])
        except (KeyError, TypeError, ValueError):
            return None
        for e in entities:
            if e.kind == "npc" and e.id == nid:
                return e.pos
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


def use_npc_type(intent: dict, block: Pos, entities: list[Any]) -> str:
    """``npc_type_code`` a ``Use`` on ``block`` hit, else empty.

    An npc-target Use is attributed only when the one NPC seen on the block is
    that target (A45); any other NPC there leaves the hit unattributed.
    """
    target = intent.get("target")
    if not isinstance(target, dict) or target.get("kind") != "npc":
        return npc_type_on_block(block, entities)
    npcs = [e for e in entities if e.kind == "npc" and e.pos == block]
    if len(npcs) != 1 or npcs[0].id != target.get("npc_id") or not npcs[0].code:
        return ""
    return npcs[0].code


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
    character could have struck: none in sight when the ``Use`` applied
    (``AppliedUse.others_in_sight``) or at the response's observation
    (``others_in_sight``), and exactly one ``NPCDamaged`` on the block and
    tick our ``Use`` resolved (docs/GAME_NOTES.md). It is
    kept as the max per NPC type seen on that block (``merge_weapon_hit``); a
    ``Use`` with no single NPC type on its block records nothing.
    """
    if not armed_code or not applied_uses or others_in_sight:
        return
    hits: dict[AppliedUse, list[int | None]] = {u: [] for u in applied_uses if u.npc_type and not u.others_in_sight}
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


def absorb_damaged_worn(
    items: dict[str, dict[str, Any]],
    events: list[dict],
    worn_codes: dict[str, str],
    removed_code: str | None,
    *entity_views: list[Any],
) -> None:
    """Record ``Damaged`` from NPCs against one worn item, or its bare baseline.

    The caller passes only a response whose worn loadout held for every tick
    in it, so ``worn_codes`` is the loadout at each hit's tick. With exactly
    one filled worn slot, the hit raises that subtype's ``damage_taken``. With
    none filled, it raises ``damage_without`` on ``removed_code`` alone: the
    subtype that was the only worn item just before the slots emptied. Any
    other loadout records nothing (PLAN.md A18).
    """
    if len(worn_codes) == 1:
        code, field = next(iter(worn_codes.values())), "damage_taken"
    elif not worn_codes and removed_code:
        code, field = removed_code, "damage_without"
    else:
        return
    for ev in events:
        if ev.get("kind") != "Damaged":
            continue
        key = type_key_from_damaged(ev, *entity_views)
        if key is None or key[0] != "npc":
            continue
        merge_worn_hit(items, code, field, key[1], damage_amount(ev))
