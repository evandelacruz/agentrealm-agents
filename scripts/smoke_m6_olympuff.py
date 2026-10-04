#!/usr/bin/env python3
"""A4: Live M6 acceptance on Olympuff (docs/PLAYABLE_AGENT_PLAN.md M6 done-when).

Walks until 200 Steps apply with no movement_cooldown rejections and
POST tick goes out in under a quarter of calm windows. Requires network access,
AGENTREALM_API_KEY, and AGENTREALM_BASE_URL (default https://api.agentrealm.gg).
Lives on live worlds are permanent; this uses a dedicated character name.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / "python"
sys.path.insert(0, str(PYTHON))

from agentrealm_agent import config  # noqa: E402
from agentrealm_agent.client import ApiError, Client  # noqa: E402
from agentrealm_agent.knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge  # noqa: E402
from agentrealm_agent.investigation import mark_cell_read, mark_npc_spoken  # noqa: E402
from agentrealm_agent.m6_acceptance import M6AcceptanceMetrics, TARGET_STEPS  # noqa: E402
from agentrealm_agent.runner import Runner  # noqa: E402
from agentrealm_agent.world import WorldModel, terrain_cells  # noqa: E402

DEFAULT_CHARACTER = PYTHON / "characters" / "olympuff_walker.toml"
DEFAULT_BASE = "https://api.agentrealm.gg"


def _find_character_id(client: Client, cfg: config.CharacterConfig) -> int | None:
    for row in client.list_characters():
        if row.get("name") == cfg.name and row.get("world_code") == cfg.world:
            return int(row["id"])
    return None


def ensure_character(client: Client, cfg: config.CharacterConfig) -> int:
    state = config.load_state(cfg)
    if state is not None:
        return int(state["character_id"])
    try:
        created = client.create_character(cfg.world, cfg.name, cfg.avatar, cfg.model_agent)
    except ApiError:
        found = _find_character_id(client, cfg)
        if found is None:
            raise
        config.save_state(cfg, {"character_id": found, "world": cfg.world})
        return found
    config.save_state(cfg, {"character_id": created["id"], "world": cfg.world})
    return int(created["id"])


class InvestigateQuietClient:
    """Marks Read/Say interest items done on perception reads (M6 smoke only)."""

    def __init__(self, inner: Client, knowledge: KnowledgeBase):
        self._inner = inner
        self._knowledge = knowledge

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            result = attr(*args, **kwargs)
            if name == "entities" and len(args) >= 2:
                map_id = int(args[1])
                for row in result.get("npcs") or []:
                    mark_npc_spoken(self._knowledge, int(row["id"]))
            elif name == "terrain" and len(args) >= 2:
                map_id = int(args[1])
                for pos, cell in terrain_cells(result).items():
                    if cell.get("readable"):
                        mark_cell_read(self._knowledge, map_id, pos)
            return result

        return call


def quiet_investigate_for_smoke(client: Client, cid: int, knowledge: KnowledgeBase) -> None:
    """Mark visible Read/Say targets done so Investigate does not block calm walking."""
    w = WorldModel(character_id=cid)
    w.apply_self(client.self_(cid))
    w.apply_position(client.position(cid))
    w.apply_terrain(client.terrain(cid, w.map_id, *w.perception_rect()))
    w.apply_entities(client.entities(cid, w.map_id, *w.perception_rect()))
    map_id = w.map_id
    if map_id is None:
        return
    for ent in w.entities:
        if ent.kind == "npc":
            mark_npc_spoken(knowledge, ent.id)
    for pos, flag in w.view.readable.items():
        if flag:
            mark_cell_read(knowledge, map_id, pos)


def run_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    *,
    target_steps: int,
    timeout_s: float,
) -> M6AcceptanceMetrics:
    stop = threading.Event()
    metrics = M6AcceptanceMetrics(target_steps=target_steps, stop=stop)
    knowledge: KnowledgeBase = load_knowledge(cfg.world)

    def out(line: str) -> None:
        print(line, flush=True)

    api = InvestigateQuietClient(client, knowledge)
    runner = Runner(
        cfg,
        metrics.wrap(api),
        cid,
        stop,
        out,
        knowledge=knowledge,
        acceptance=metrics,
    )

    def watchdog() -> None:
        if timeout_s <= 0:
            return
        if stop.wait(timeout_s):
            return
        out(f"[{cfg.name}] timeout after {timeout_s:.0f}s with {metrics.steps_applied} steps")
        stop.set()

    thread = threading.Thread(target=runner.run, daemon=True)
    wd = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    wd.start()
    thread.join()
    stop.set()
    try:
        save_knowledge(knowledge)
    except OSError as e:
        out(f"knowledge base {knowledge.world_code}: not saved: {e}")
    return metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M6 acceptance smoke test on Olympuff (A4).")
    ap.add_argument(
        "--character",
        type=Path,
        default=DEFAULT_CHARACTER,
        help="character TOML (default: python/characters/olympuff_walker.toml)",
    )
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    ap.add_argument("--steps", type=int, default=TARGET_STEPS, help="applied Step count to reach")
    ap.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="wall-clock seconds before giving up (0 = no limit)",
    )
    args = ap.parse_args(argv)

    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    try:
        cfg = config.load(args.character)
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if cfg.world != "olympuff":
        print(f"expected world olympuff, got {cfg.world!r}", file=sys.stderr)
        return 2

    client = Client(args.base_url, args.api_key)
    try:
        cid = ensure_character(client, cfg)
    except ApiError as e:
        print(f"create: {e}", file=sys.stderr)
        return 2

    knowledge = load_knowledge(cfg.world)
    try:
        quiet_investigate_for_smoke(client, cid, knowledge)
        save_knowledge(knowledge)
    except ApiError as e:
        print(f"investigate quiet: {e}", file=sys.stderr)
        return 2

    print(
        f"M6 smoke (A4): {cfg.name} ({cid}) on {cfg.world} "
        f"→ {args.steps} steps, base {args.base_url}",
        flush=True,
    )
    started = time.monotonic()
    metrics = run_smoke(
        client,
        cfg,
        cid,
        target_steps=args.steps,
        timeout_s=args.timeout,
    )
    elapsed = time.monotonic() - started
    print(f"finished in {elapsed:.1f}s", flush=True)
    for line in metrics.summary_lines():
        print(line, flush=True)
    failures = metrics.failures()
    if failures:
        print("FAIL:", "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: M6 acceptance criteria met", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
