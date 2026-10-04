"""Single intents the states build (A5)."""

from __future__ import annotations

from ..world import Entity, Pos


def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def use_on(e: Entity) -> dict:
    return {"verb": "Use", "target": {"kind": "character", "character_id": e.id}}


def use_block(p: Pos) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": p[0], "y": p[1]}}


def take(supply_id: int) -> dict:
    return {"verb": "Take", "supply_id": supply_id}


def withdraw_all(chest_id: int) -> dict:
    # No supply_ids: take everything that fits, lowest ids first (B117).
    return {"verb": "WithdrawFromChest", "chest_id": chest_id}


def drop(supply_id: int) -> dict:
    return {"verb": "Drop", "supply_id": supply_id}


def withdraw(chest_id: int, supply_ids: list[int]) -> dict:
    # All or nothing: carry_capacity_full when one too many (API WithdrawFromChest).
    return {"verb": "WithdrawFromChest", "chest_id": chest_id, "supply_ids": list(supply_ids)}


def arm(supply_id: int) -> dict:
    return {"verb": "Arm", "supply_id": supply_id}


def use_self(character_id: int) -> dict:
    return {"verb": "Use", "target": {"kind": "character", "character_id": character_id}}
