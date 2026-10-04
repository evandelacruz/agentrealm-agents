#!/usr/bin/env python3
"""A18: bounded live session to compare Damaged with and without one worn armor piece.

Uses sandbox, a dedicated character, and the public API. Requires
AGENTREALM_API_KEY and AGENTREALM_BASE_URL. Does not commit credentials.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
sys.path.insert(0, str(PYTHON))

from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.config import CharacterConfig, Policy, load_state, save_state  # noqa: E402
from agentrealm_agent.item_table import absorb_damaged_worn, loadout_from_inventory  # noqa: E402
from agentrealm_agent.knowledge_base import load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.threat import type_key_from_damaged  # noqa: E402
from agentrealm_agent.world import WorldModel  # noqa: E402

CHAR_FILE = PYTHON / "characters" / "a18_armor_probe.toml"
MAX_TICKS = 280
TARGET_NPC = "chugbug"
ARMOR_CODE = "head"
ARMOR_SLOT = "head"
PERCEPTION = 11


def client() -> Client:
    base = os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg").rstrip("/")
    key = os.environ.get("AGENTREALM_API_KEY")
    if not key:
        raise SystemExit("AGENTREALM_API_KEY is required")
    return Client(base, key)


def cfg() -> CharacterConfig:
    return CharacterConfig(
        "CursorA18Armor8774",
        "default",
        "agentrealm-reference/scripted",
        "sandbox",
        Policy(kind="scripted", goals=["hold"], on_hostile="ignore", hostile=["npc"], hostile_range=6),
        CHAR_FILE,
    )


def ensure_character(c: Client) -> int:
    character = cfg()
    state = load_state(character)
    if state is not None:
        return int(state["character_id"])
    created = c.create_character(character.world, character.name, character.avatar, character.model_agent)
    save_state(character, {"character_id": created["id"], "world": character.world})
    return int(created["id"])


def entity_read(c: Client, cid: int, w: WorldModel) -> None:
    x0 = w.pos[0] - PERCEPTION
    y0 = w.pos[1] - PERCEPTION
    payload = c.entities(cid, w.map_id, x0, y0, PERCEPTION * 2 + 1, PERCEPTION * 2 + 1)
    w.apply_entities(payload)


def supply_id(inv: dict, code: str) -> int | None:
    for entry in inv.get("held") or []:
        if isinstance(entry, dict) and entry.get("supply_subtype_code") == code:
            return int(entry["id"])
    for entry in (inv.get("worn") or {}).values():
        if isinstance(entry, dict) and entry.get("supply_subtype_code") == code:
            return int(entry["id"])
    return None


def paced_tick(c: Client, cid: int, w: WorldModel, intent: dict | None) -> dict:
    for attempt in range(6):
        try:
            r = c.tick(cid, intent, snapshot_version=w.snapshot_version)
            time.sleep(0.12)
            return r
        except ApiError as e:
            if e.rate_limited and attempt < 5:
                time.sleep(max(0.5, (e.retry_after or 1.0)))
                continue
            raise
    raise RuntimeError("paced_tick exhausted retries")


def _step_direction(dx: int, dy: int) -> str:
    vert = "up" if dy < 0 else "down" if dy > 0 else ""
    horiz = "left" if dx < 0 else "right" if dx > 0 else ""
    if vert and horiz:
        return f"{vert}_{horiz}"
    return vert or horiz or "right"


def step_toward(c: Client, cid: int, w: WorldModel, tx: int, ty: int) -> dict:
    dx = max(-1, min(1, tx - w.pos[0]))
    dy = max(-1, min(1, ty - w.pos[1]))
    if dx == 0 and dy == 0:
        return paced_tick(c, cid, w, {"verb": "Wait"})
    return paced_tick(c, cid, w, {"verb": "Step", "direction": _step_direction(dx, dy)})


def apply_round(w: WorldModel, r: dict, kb_items: dict) -> list[dict]:
    earlier = list(w.entities)
    events = w.apply_events(r.get("events_by_tick") or [])
    w.apply_observation(r.get("observation"))
    if "tick" in r:
        w.tick = int(r["tick"])
    absorb_damaged_worn(kb_items, events, w.worn_codes, w.entities, earlier)
    return events


def run_probe() -> dict:
    c = client()
    cid = ensure_character(c)
    kb = load_knowledge("sandbox")
    w = WorldModel(cid)
    try:
        pos = c.position(cid)
        w.map_id = int(pos["map_id"])
        w.pos = (int(pos["x"]), int(pos["y"]))
        r = paced_tick(c, cid, w, None)
    except Exception:
        w.snapshot_version = None
        r = paced_tick(c, cid, w, None)
    apply_round(w, r, kb.items)
    entity_read(c, cid, w)

    hits_worn: list[dict] = []
    hits_bare: list[dict] = []
    wearing = False
    toggled_once = False

    for n in range(MAX_TICKS):
        events: list[dict] = []
        if n % 8 == 0:
            entity_read(c, cid, w)

        obs = r.get("observation")
        inv: dict = {}
        if obs:
            body = obs.get("snapshot") if obs.get("complete") else obs.get("delta")
            if isinstance(body, dict) and isinstance(body.get("inventory"), dict):
                inv = body["inventory"]
        armor_id = supply_id(inv, ARMOR_CODE) if inv else None
        if armor_id is None:
            for s in w.held_supplies:
                if s.code == ARMOR_CODE:
                    armor_id = s.id
                    break
        for code in w.worn_codes.values():
            if code == ARMOR_CODE and armor_id is None:
                for s in w.held_supplies:
                    if s.code == ARMOR_CODE:
                        armor_id = s.id

        gems = int(inv.get("gems", 0)) if inv else 0
        for s in w.entities:
            if s.kind == "supply" and s.code == ARMOR_CODE:
                if armor_id is None and gems >= 3:
                    r = paced_tick(c, cid, w, {"verb": "Take", "supply_id": s.id})
                    events = apply_round(w, r, kb.items)
                    break
                if armor_id is None and gems < 3:
                    r = paced_tick(
                        c,
                        cid,
                        w,
                        {"verb": "Use", "target": {"kind": "block", "x": w.pos[0], "y": w.pos[1]}},
                    )
                    events = apply_round(w, r, kb.items)
                    break
        else:
            rats = [e for e in w.entities if e.kind == "npc" and e.code == TARGET_NPC]
            if rats and armor_id is not None:
                rat = min(rats, key=lambda e: abs(e.pos[0] - w.pos[0]) + abs(e.pos[1] - w.pos[1]))
                dist = abs(rat.pos[0] - w.pos[0]) + abs(rat.pos[1] - w.pos[1])
                if dist > 1:
                    r = step_toward(c, cid, w, rat.pos[0], rat.pos[1])
                elif not wearing:
                    r = paced_tick(c, cid, w, {"verb": "Wear", "supply_id": armor_id, "slot": ARMOR_SLOT})
                    wearing = True
                elif wearing and not toggled_once:
                    r = paced_tick(c, cid, w, {"verb": "Remove", "slot": ARMOR_SLOT})
                    wearing = False
                    toggled_once = True
                else:
                    r = paced_tick(c, cid, w, {"verb": "Wait"})
                events = apply_round(w, r, kb.items)
            elif rats:
                rat = min(rats, key=lambda e: abs(e.pos[0] - w.pos[0]) + abs(e.pos[1] - w.pos[1]))
                r = step_toward(c, cid, w, rat.pos[0], rat.pos[1])
                events = apply_round(w, r, kb.items)
            else:
                r = paced_tick(c, cid, w, {"verb": "Step", "direction": "right"})
                events = apply_round(w, r, kb.items)

        for ev in events:
            if ev.get("kind") != "Damaged":
                continue
            key = type_key_from_damaged(ev, w.entities)
            if key is None or key[0] != "npc" or key[1] != TARGET_NPC:
                continue
            row = {"tick": ev.get("tick"), "amount": ev.get("amount"), "worn": dict(w.worn_codes)}
            if len(w.worn_codes) == 1:
                hits_worn.append(row)
            elif len(w.worn_codes) == 0:
                hits_bare.append(row)

        if hits_worn and hits_bare:
            break

    save_knowledge(kb)
    return {
        "items": kb.items.get(ARMOR_CODE, {}),
        "hits_worn": hits_worn,
        "hits_bare": hits_bare,
        "character_id": cid,
        "final_pos": w.pos,
    }


if __name__ == "__main__":
    print(json.dumps(run_probe(), indent=2))
