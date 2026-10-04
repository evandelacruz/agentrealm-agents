#!/usr/bin/env python3
"""A20: Observe heart/gem supply codes and stowed Drop on the public API.

Writes a redacted note to docs/observations/A20_live_play.md. Requires
AGENTREALM_API_KEY and AGENTREALM_BASE_URL. Uses the dedicated character in
python/characters/a20_loot_observer.toml (lives on Olympuff are permanent).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.world import WorldModel, terrain_cells  # noqa: E402

CHARACTER = PYTHON / "characters" / "a20_loot_observer.toml"
OUT = REPO / "docs" / "observations" / "A20_live_play.md"
WINDOW = 0.11  # one HTTP call per sim tick window (~10 Hz)
MAX_GRASS_USES = 400


def paced_call(client: Client, fn, *args, **kwargs):
    while True:
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            if e.rate_limited:
                time.sleep(max(WINDOW, e.retry_after or 1.0))
                continue
            raise
        finally:
            time.sleep(WINDOW)


def ensure_character(client: Client, cfg: config.CharacterConfig) -> int:
    state = config.load_state(cfg)
    if state is not None:
        return int(state["character_id"])
    created = client.create_character(cfg.world, cfg.name, cfg.avatar, cfg.model_agent)
    config.save_state(cfg, {"character_id": created["id"], "world": cfg.world})
    return int(created["id"])


def inv_summary(snap: dict | None) -> dict[str, Any]:
    if not snap or not isinstance(snap.get("inventory"), dict):
        return {}
    inv = snap["inventory"]
    out: dict[str, Any] = {
        "gems": inv.get("gems"),
        "lives": snap.get("lives"),
        "held": [
            {"id": s.get("id"), "code": s.get("supply_subtype_code")}
            for s in inv.get("held") or []
            if isinstance(s, dict)
        ],
        "chest": [
            {"id": s.get("id"), "code": s.get("supply_subtype_code")}
            for s in inv.get("chest") or []
            if isinstance(s, dict)
        ],
    }
    armed = inv.get("armed")
    if isinstance(armed, dict):
        out["armed"] = {"id": armed.get("id"), "code": armed.get("supply_subtype_code")}
    return out


def supplies_on_ground(entities: dict) -> list[dict]:
    out = []
    for s in entities.get("supplies") or []:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": s.get("id"),
                "code": s.get("supply_subtype_code"),
                "x": s.get("x"),
                "y": s.get("y"),
                "gem_price": s.get("gem_price"),
            }
        )
    return out


def sync_reads(client: Client, cid: int, w: WorldModel) -> None:
    s = paced_call(client, client.self_, cid)
    w.apply_self(s)
    p = paced_call(client, client.position, cid)
    w.apply_position(p)
    assert w.map_id is not None and w.pos is not None
    t = paced_call(client, client.terrain, cid, w.map_id, *w.perception_rect())
    w.apply_terrain(t)
    e = paced_call(client, client.entities, cid, w.map_id, *w.perception_rect())
    w.apply_entities(e)


def tick(client: Client, cid: int, w: WorldModel, intent: dict | None) -> dict:
    body = paced_call(client, client.tick, cid, intent, snapshot_version=w.snapshot_version)
    w.apply_observation(body.get("observation") or {})
    if "events" in body:
        w.apply_events(body["events"])
    return body


def grass_here(w: WorldModel) -> tuple[int, int] | None:
    assert w.pos is not None
    if w.view.tiles.get(w.pos) == "grass":
        return w.pos
    for (x, y), block in w.view.tiles.items():
        if block == "grass":
            return (x, y)
    return None


def step_to(client: Client, cid: int, w: WorldModel, goal: tuple[int, int]) -> None:
    assert w.pos is not None
    while w.pos != goal:
        dx = max(-1, min(1, goal[0] - w.pos[0]))
        dy = max(-1, min(1, goal[1] - w.pos[1]))
        nx, ny = w.pos[0] + dx, w.pos[1] + dy
        body = tick(client, cid, w, {"verb": "SetPosition", "x": nx, "y": ny})
        if body.get("outcome") == "rejected":
            break
        sync_reads(client, cid, w)


def main() -> int:
    api_key = os.environ.get("AGENTREALM_API_KEY", "")
    base = os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg")
    if not api_key:
        print("set AGENTREALM_API_KEY", file=sys.stderr)
        return 1

    cfg = config.load(CHARACTER)
    client = Client(base, api_key, timeout=30.0)
    cid = ensure_character(client, cfg)
    w = WorldModel(cid)
    log: list[str] = []
    findings: dict[str, Any] = {}

    def note(line: str) -> None:
        print(line, flush=True)
        log.append(line)

    try:
        sync_reads(client, cid, w)
        note(f"character_id={cid} map={w.map_id} pos={w.pos} lives={w.lives}")

        # Confirm ground gem code from a free town supply if visible.
        e = paced_call(client, client.entities, cid, w.map_id, *w.perception_rect())
        w.apply_entities(e)
        for s in supplies_on_ground(e):
            if s.get("code") == "gem" and s.get("gem_price") is None and s.get("id"):
                before = {"gems": w.gems, "lives": w.lives}
                body = tick(client, cid, w, {"verb": "Take", "supply_id": int(s["id"])})
                after = inv_summary(body.get("observation", {}).get("snapshot"))
                findings["gem_take"] = {
                    "supply_code": "gem",
                    "supply_id_redacted": True,
                    "before": before,
                    "after": after,
                    "outcome": body.get("outcome"),
                }
                note(f"gem Take: before gems={before.get('gems')} after gems={after.get('gems')}")
                break

        sync_reads(client, cid, w)

        # Cut grass until a heart/life supply or enough attempts.
        heart_event: dict | None = None
        for n in range(MAX_GRASS_USES):
            g = grass_here(w)
            if g is None:
                sync_reads(client, cid, w)
                g = grass_here(w)
            if g is None:
                note("no grass in perception; stepping randomly")
                assert w.pos is not None
                tick(client, cid, w, {"verb": "SetPosition", "x": w.pos[0] + 1, "y": w.pos[1]})
                sync_reads(client, cid, w)
                continue
            if g != w.pos:
                step_to(client, cid, w, g)
            before_lives = w.lives
            before_gems = w.gems
            body = tick(client, cid, w, {"verb": "Use", "target": {"kind": "block", "x": g[0], "y": g[1]}})
            e = paced_call(client, client.entities, cid, w.map_id, *w.perception_rect())
            w.apply_entities(e)
            for s in supplies_on_ground(e):
                code = s.get("code") or ""
                if code == "gem":
                    findings.setdefault("grass_gem_drop", []).append({"code": code, "attempt": n + 1})
                if code not in ("gem", "apple", "berry", "golden_cap") and s.get("id"):
                    # Possible life drop — try Take if lives might increase.
                    inv_before = inv_summary(body.get("observation", {}).get("snapshot"))
                    take_body = tick(client, cid, w, {"verb": "Take", "supply_id": int(s["id"])})
                    inv_after = inv_summary(take_body.get("observation", {}).get("snapshot"))
                    if (inv_after.get("lives") or 0) > (inv_before.get("lives") or before_lives or 0):
                        heart_event = {
                            "supply_code": code,
                            "attempt": n + 1,
                            "before_lives": before_lives,
                            "after_lives": inv_after.get("lives"),
                            "outcome": take_body.get("outcome"),
                        }
                        note(f"life pickup code={code} lives {before_lives} -> {inv_after.get('lives')}")
                        break
            if heart_event:
                findings["heart_take"] = heart_event
                break
            if n and n % 50 == 0:
                note(f"grass uses={n} lives={w.lives} gems={w.gems}")

        # Stowed Drop: pick up apple/berry if present, deposit, drop.
        sync_reads(client, cid, w)
        e = paced_call(client, client.entities, cid, w.map_id, *w.perception_rect())
        w.apply_entities(e)
        apple = next(
            (s for s in supplies_on_ground(e) if s.get("code") in ("apple", "berry") and s.get("id")),
            None,
        )
        if apple:
            tick(client, cid, w, {"verb": "Take", "supply_id": int(apple["id"])})
            sync_reads(client, cid, w)
            inv = w  # refresh via tick
            body0 = paced_call(client, client.tick, cid, None, snapshot_version=w.snapshot_version)
            w.apply_observation(body0.get("observation") or {})
            held = [
                s for s in (body0.get("observation", {}).get("snapshot", {}).get("inventory", {}).get("held") or [])
                if isinstance(s, dict) and s.get("id")
            ]
            if held:
                sid = int(held[0]["id"])
                dep = tick(client, cid, w, {"verb": "DepositToChest", "supply_id": sid})
                sync_reads(client, cid, w)
                body1 = paced_call(client, client.tick, cid, None, snapshot_version=w.snapshot_version)
                w.apply_observation(body1.get("observation") or {})
                stowed = [
                    s
                    for s in (
                        body1.get("observation", {}).get("snapshot", {}).get("inventory", {}).get("chest") or []
                    )
                    if isinstance(s, dict) and s.get("id")
                ]
                if stowed:
                    stid = int(stowed[0]["id"])
                    drop_body = tick(client, cid, w, {"verb": "Drop", "supply_id": stid})
                    findings["stowed_drop"] = {
                        "deposit_outcome": dep.get("outcome"),
                        "drop_outcome": drop_body.get("outcome"),
                        "drop_rejection": (drop_body.get("rejection") or {}).get("code"),
                    }
                    note(
                        f"stowed Drop: outcome={drop_body.get('outcome')} "
                        f"code={(drop_body.get('rejection') or {}).get('code')}"
                    )
        else:
            note("no apple/berry in sight for stowed Drop test")

    except ApiError as e:
        note(f"API halted: {e}")
        findings["api_error"] = str(e)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "# A20 live play (redacted)\n\n"
        f"Character: `{cfg.name}` on `{cfg.world}` (id `{cid}`).\n\n"
        "## Findings\n\n"
        f"```json\n{json.dumps(findings, indent=2)}\n```\n\n"
        "## Session log\n\n"
        + "\n".join(f"- {line}" for line in log)
        + "\n",
        encoding="utf-8",
    )
    note(f"wrote {OUT}")
    return 2 if findings.get("api_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
