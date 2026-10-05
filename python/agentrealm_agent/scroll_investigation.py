"""Scroll subtype discovery and read memory (A56).

Learns which ``supply_subtype_code`` values are scrolls by logging every code
seen, ``Read``ing one supply of each unseen code once, and keeping codes that
do not answer ``nothing_to_read`` (GAME_NOTES open questions). Unread scrolls
carried or in sight are nominated for free ``Read`` like readable cells (A30).

Knowledge-base keys in ``kb.extra`` (PLAYABLE_AGENT_PLAN Knowledge base):

- ``seen_supply_codes``: every subtype code logged from entity reads and inventory;
- ``probed_supply_codes``: codes probed once with ``Read {kind: supply}``;
- ``scroll_subtype_codes``: probed codes whose read applied (scrolls);
- ``read_supplies``: supply ids whose scroll ``Read`` applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase
from .world import Pos, WorldModel

SEEN_SUPPLY_CODES_KEY = "seen_supply_codes"
PROBED_SUPPLY_CODES_KEY = "probed_supply_codes"
SCROLL_SUBTYPE_CODES_KEY = "scroll_subtype_codes"
READ_SUPPLIES_KEY = "read_supplies"


def _str_list(kb: KnowledgeBase, key: str) -> list[str]:
    raw = kb.extra.get(key)
    return list(raw) if isinstance(raw, list) else []


def _int_list(kb: KnowledgeBase, key: str) -> list[int]:
    raw = kb.extra.get(key)
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _append_unique_str(kb: KnowledgeBase, key: str, values: Iterable[str]) -> None:
    have = _str_list(kb, key)
    seen = set(have)
    for code in values:
        if isinstance(code, str) and code and code not in seen:
            have.append(code)
            seen.add(code)
    kb.extra[key] = have


def _append_unique_int(kb: KnowledgeBase, key: str, value: int) -> None:
    have = _int_list(kb, key)
    if value not in have:
        have.append(value)
    kb.extra[key] = have


def log_supply_codes_seen(kb: KnowledgeBase | None, codes: Iterable[str]) -> None:
    """Append newly seen ``supply_subtype_code`` values (A56)."""
    if kb is None:
        return
    with kb.lock:
        _append_unique_str(kb, SEEN_SUPPLY_CODES_KEY, codes)


def seen_supply_codes(kb: KnowledgeBase | None) -> set[str]:
    if kb is None:
        return set()
    with kb.lock:
        return set(_str_list(kb, SEEN_SUPPLY_CODES_KEY))


def probed_supply_codes(kb: KnowledgeBase | None) -> set[str]:
    if kb is None:
        return set()
    with kb.lock:
        return set(_str_list(kb, PROBED_SUPPLY_CODES_KEY))


def scroll_subtype_codes(kb: KnowledgeBase | None) -> set[str]:
    if kb is None:
        return set()
    with kb.lock:
        return set(_str_list(kb, SCROLL_SUBTYPE_CODES_KEY))


def code_was_probed(kb: KnowledgeBase | None, code: str) -> bool:
    if not code:
        return False
    return code in probed_supply_codes(kb)


def mark_code_probed(kb: KnowledgeBase | None, code: str) -> None:
    if kb is None or not code:
        return
    with kb.lock:
        _append_unique_str(kb, PROBED_SUPPLY_CODES_KEY, [code])


def mark_scroll_subtype(kb: KnowledgeBase | None, code: str) -> None:
    if kb is None or not code:
        return
    with kb.lock:
        _append_unique_str(kb, SCROLL_SUBTYPE_CODES_KEY, [code])
        _append_unique_str(kb, PROBED_SUPPLY_CODES_KEY, [code])


def supply_was_read(kb: KnowledgeBase | None, supply_id: int) -> bool:
    if kb is None:
        return False
    with kb.lock:
        return supply_id in _int_list(kb, READ_SUPPLIES_KEY)


def mark_supply_read(kb: KnowledgeBase | None, supply_id: int) -> None:
    if kb is None:
        return
    with kb.lock:
        _append_unique_int(kb, READ_SUPPLIES_KEY, supply_id)


def codes_from_entities_payload(entities: dict | None) -> list[str]:
    """Subtype codes from a full entity read or a snapshot/delta ``entities`` patch."""
    if not entities:
        return []
    out: list[str] = []
    raw = entities.get("supplies")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                code = entry.get("supply_subtype_code")
                if isinstance(code, str) and code:
                    out.append(code)
    elif isinstance(raw, dict):
        for part in ("added", "changed"):
            for entry in raw.get(part) or []:
                if isinstance(entry, dict):
                    code = entry.get("supply_subtype_code")
                    if isinstance(code, str) and code:
                        out.append(code)
    return out


def codes_from_inventory_supplies(supplies: Iterable[InventorySupply]) -> list[str]:
    return [s.code for s in supplies if s.code]


def supply_code_on_world(w: WorldModel, supply_id: int) -> str:
    """Best-effort subtype for a supply id from the current model."""
    for s in w.held_supplies + w.chest_supplies:
        if s.id == supply_id and s.code:
            return s.code
    for e in w.entities:
        if e.kind == "supply" and e.id == supply_id and e.code:
            return e.code
    return ""


@dataclass(frozen=True)
class SupplyReadTarget:
    supply_id: int
    code: str
    pos: Pos | None  # None when carried in held or chest


def iter_supply_targets(
    w: WorldModel,
    in_sight_fn: Callable[..., bool],
) -> list[SupplyReadTarget]:
    """Carried supplies and ground supplies in sight, with subtype codes."""
    out: list[SupplyReadTarget] = []
    here = w.pos
    map_id = w.map_id
    for s in w.held_supplies + w.chest_supplies:
        if s.code:
            out.append(SupplyReadTarget(s.id, s.code, None))
    if here is not None and map_id is not None:
        for e in w.entities:
            if e.kind == "supply" and e.code and in_sight_fn(w, map_id, here, e.pos):
                out.append(SupplyReadTarget(e.id, e.code, e.pos))
    return out
