"""python -m agentrealm_agent create|run|status <character.toml> ..."""

from __future__ import annotations

import argparse
import os
import sys
import threading

from . import config
from .client import ApiError, Client
from .runner import Runner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentrealm_agent", description="Reference agent for Agent Realm.")
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", "https://api.agentrealm.gg"))
    ap.add_argument("--api-key", default=os.environ.get("AGENTREALM_API_KEY", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (
        ("create", "create each character in its world and save its id"),
        ("run", "drive each character until interrupted"),
        ("status", "print each character's self and position"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("characters", nargs="+", help="character .toml files")
    args = ap.parse_args(argv)

    if not args.api_key:
        print("set AGENTREALM_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    try:
        cfgs = [config.load(p) for p in args.characters]
    except (config.ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    client = Client(args.base_url, args.api_key)
    return {"create": create, "run": run, "status": status}[args.cmd](client, cfgs)


def create(client: Client, cfgs: list[config.CharacterConfig]) -> int:
    failed = 0
    for cfg in cfgs:
        if (state := config.load_state(cfg)) is not None:
            print(f"{cfg.name}: already created as {state['character_id']}")
            continue
        try:
            s = client.create_character(cfg.world, cfg.name, cfg.avatar, cfg.model_agent)
        except ApiError as e:
            print(f"{cfg.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        config.save_state(cfg, {"character_id": s["id"], "world": cfg.world})
        print(f"{cfg.name}: created {s['id']} in {cfg.world}")
    return 1 if failed else 0


def _ids(cfgs: list[config.CharacterConfig]) -> list[tuple[config.CharacterConfig, int]] | None:
    out = []
    for cfg in cfgs:
        state = config.load_state(cfg)
        if state is None:
            print(f"{cfg.name}: not created yet; run `create` first", file=sys.stderr)
            return None
        out.append((cfg, int(state["character_id"])))
    return out


def status(client: Client, cfgs: list[config.CharacterConfig]) -> int:
    ids = _ids(cfgs)
    if ids is None:
        return 2
    for cfg, cid in ids:
        try:
            s = client.self_(cid)
            print(f"{cfg.name} ({cid}): lives={s['lives']} alive={s['alive']} placed={s['placed']}")
        except ApiError as e:
            print(f"{cfg.name} ({cid}): {e}", file=sys.stderr)
    return 0


def run(client: Client, cfgs: list[config.CharacterConfig]) -> int:
    ids = _ids(cfgs)
    if ids is None:
        return 2
    stop = threading.Event()
    lock = threading.Lock()

    def out(line: str) -> None:
        with lock:
            print(line, flush=True)

    def drive(cfg: config.CharacterConfig, cid: int) -> None:
        try:
            Runner(cfg, client, cid, stop, out).run()
        except ApiError as e:
            out(f"[{cfg.name}] stopped: {e}")
        except Exception as e:  # keep the other characters running
            out(f"[{cfg.name}] crashed: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=drive, args=(cfg, cid), daemon=True) for cfg, cid in ids]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        out("stopping; characters stay in the world where they stand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
