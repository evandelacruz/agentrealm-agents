"""Intent dict builders for ``POST tick`` (A5).

Each helper returns one API intent; states usually return a one-element list.
Field names match https://agentrealm.gg/docs/api — wrong shapes are rejected at ingest.
"""

from __future__ import annotations

from ..world import Entity, Pos


def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def use_on(e: Entity) -> dict:
    return {"verb": "Use", "target": {"kind": "character", "character_id": e.id}}


def use_npc(npc: Entity) -> dict:
    # The server resolves the block the NPC stands on the tick the Use runs (B126),
    # so a queued swing follows an NPC that moved after the plan.
    return {"verb": "Use", "target": {"kind": "npc", "npc_id": npc.id}}


def use_block(p: Pos) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": p[0], "y": p[1]}}


def take(supply_id: int) -> dict:
    return {"verb": "Take", "supply_id": supply_id}


def withdraw_all(chest_id: int) -> dict:
    # No supply_ids: take everything that fits, lowest ids first (B117).
    return {"verb": "WithdrawFromChest", "chest_id": chest_id}


def read_block(pos: Pos) -> dict:
    # A sign target is exactly {kind, x, y} on the character's own map; a field
    # the verb does not take is malformed_intent at ingest (API rules § Read).
    return {"verb": "Read", "target": {"kind": "block", "x": pos[0], "y": pos[1]}}


def read_supply(supply_id: int) -> dict:
    return {"verb": "Read", "target": {"kind": "supply", "supply_id": supply_id}}


def say_to(npc: Entity, text: str = "hello") -> dict:
    # Say names its recipient by a top-level npc_id (or character_id), not a
    # target; anything else is malformed_intent at ingest (API rules § Say).
    return {"verb": "Say", "npc_id": npc.id, "text": text}


def drop(supply_id: int) -> dict:
    return {"verb": "Drop", "supply_id": supply_id}


def withdraw(chest_id: int, supply_ids: list[int]) -> dict:
    # All or nothing: carry_capacity_full when one too many (API WithdrawFromChest).
    return {"verb": "WithdrawFromChest", "chest_id": chest_id, "supply_ids": list(supply_ids)}


def arm(supply_id: int) -> dict:
    return {"verb": "Arm", "supply_id": supply_id}


def wear(supply_id: int) -> dict:
    return {"verb": "Wear", "supply_id": supply_id}


def remove_slot(slot: str) -> dict:
    return {"verb": "Remove", "slot": slot}


def compose(supply_ids: list[int]) -> dict:
    return {"verb": "Compose", "supply_ids": list(supply_ids)}


def use_self(character_id: int) -> dict:
    return {"verb": "Use", "target": {"kind": "character", "character_id": character_id}}
