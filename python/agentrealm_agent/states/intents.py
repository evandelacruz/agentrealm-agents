"""Single intents the states build (A5)."""

from __future__ import annotations

from ..world import Entity, Pos


def set_position(p: Pos) -> dict:
    return {"verb": "SetPosition", "x": p[0], "y": p[1]}


def use_on(e: Entity) -> dict:
    return {"verb": "Use", "target": {"kind": "character", "character_id": e.id}}


def take(e: Entity) -> dict:
    return {"verb": "Take", "supply_id": e.id}


def withdraw_all(chest_id: int) -> dict:
    # No supply_ids: take everything that fits, lowest ids first (B117).
    return {"verb": "WithdrawFromChest", "chest_id": chest_id}
