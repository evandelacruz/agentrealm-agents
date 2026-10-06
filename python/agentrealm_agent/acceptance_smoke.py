"""Shared helpers for live acceptance smoke scripts (M7 A16, M8 A25, M9 A29, M10 A33, M11 A40)."""

from __future__ import annotations

import argparse
import threading
import time
from typing import Callable

from . import config
from .acceptance import AcceptanceHooks, ParkSplit
from .client import ApiError, Client
from .executor.intents import wait
from .knowledge_base import KnowledgeBase, load as load_knowledge, save as save_knowledge
from .park import ABORT_JOIN_SECONDS, DEFAULT_PARK_SECONDS, PARK_DIED, PARK_SECONDS_HELP, install_stop_signals
from .runner import Runner
from .strategist import Strategist

DEFAULT_BASE = "https://api.agentrealm.gg"
NO_PLANNER_HELP = "test mode: play without the AI planner (A35)"
STOP_ON_DEATH_HELP = "fail the run and end it at the first death; by default deaths are counted and reported and play goes on after the respawn"


def add_park_argument(ap: argparse.ArgumentParser) -> None:
    """``--park-seconds``: the park phase after the run (A66)."""
    ap.add_argument("--park-seconds", type=float, default=DEFAULT_PARK_SECONDS, help=PARK_SECONDS_HELP)


def alive_at_end_failures(
    client: Client, cid: int, metrics: ParkSplit, *, stop_on_death: bool, out: Callable[[str], None] = print
) -> list[str]:
    """The end-of-run alive check, as gate failures.

    Only the no-death gate (``--stop-on-death``) fails a run that ends dead;
    otherwise a dead character is reported and left to respawn. A death in
    the park phase (A66) belongs to the park, which is reported on its own
    line (``ParkSplit.park_summary_line``), never to the scenario.
    """
    try:
        alive = client.self_(cid).get("alive", True)
    except ApiError as e:
        return [f"self read failed: {e.code}"]
    if alive:
        return []
    if metrics.park is not None and metrics.park.outcome == PARK_DIED:
        out("character not alive at end (died while parking; respawning)")
        return []
    if stop_on_death:
        return ["character not alive at end"]
    out("character not alive at end (respawning)")
    return []


def planner_for(no_planner: bool) -> Strategist:
    """The AI planner for a live run, or the ``--no-planner`` test mode.

    Raises ``PlannerConfigError`` (one line) when it is on and has no key,
    and ``PlannerAuthError`` when the provider refuses the key on the one
    check call: call it before touching the character, so the run fails fast.
    """
    if no_planner:
        return Strategist.off()
    planner = Strategist.from_env()
    planner.check()
    return planner


def pin_goto(cfg: config.CharacterConfig, map_id: int, target: tuple[int, int], *, planner_on: bool) -> None:
    """Send the agent to ``target`` on ``map_id`` before anything else.

    With the planner on, the target is a directives goal (``travel:point``),
    which the planner cannot override; it keeps planning below it, and owns
    the stack once the agent stands there. In the ``--no-planner`` test mode
    it is the built-in plan's ``goto``, in front of the profile's goals.
    """
    if planner_on:
        cfg.pinned_goals = [f"travel:point:{map_id}:{target[0]}:{target[1]}"]
        return
    cfg.policy.goto, cfg.policy.goto_map = target, map_id
    cfg.policy.goals = ["goto"] + [g for g in cfg.policy.goals if g != "goto"]

# Self reads before giving up on a character that stays asleep or downed, one
# a second: well inside the call budget, and longer than the 5 s respawn delay.
WAKE_READS = 30
# What to do about the known wake rejections (GAME_NOTES Sleep). Any other
# rejected wake ``Wait`` also stops the start, with its code.
WAKE_REFUSALS = {
    "alive_cap_full": "the world's alive cap is full; wait for a slot and re-run",
    "block_occupied": "no free block to wake on; wait and re-run",
}


def wake(client: Client, cid: int, *, pause: Callable[[float], None] = time.sleep) -> None:
    """Get the character awake and alive before its position is read.

    A sleeping character (asleep after 10 idle minutes) is off the map and
    has no position. Any intent wakes it; ``Wait`` is the one that does
    nothing else (GAME_NOTES Sleep). A downed one respawns after
    ``respawn_delay_seconds``, so this reads self until it is alive.
    Raises ValueError saying why it could not.

    This repeats Sync's wake (``states/sync.py``) because acceptance scripts
    need a position before the runner starts; keep the two in step.
    """
    for _ in range(WAKE_READS):
        s = client.self_(cid)
        if not s.get("alive", True):
            pause(1.0)
            continue
        if not s.get("asleep"):
            return
        reply = client.tick(cid, [wait()])
        for result in reply.get("intent_results") or []:
            if result.get("outcome") == "rejected":
                code = (result.get("rejection") or {}).get("code") or "unknown"
                raise ValueError(f"cannot wake: {code}: {WAKE_REFUSALS.get(code, 'wake Wait rejected')}")
        pause(1.0)
    raise ValueError(f"still asleep or downed after {WAKE_READS} self reads")


def navigation_start(client: Client, cid: int) -> tuple[int, tuple[int, int]]:
    """The overworld's map id and where the character stands on it.

    Raises ValueError when the character is not on the overworld, where M7, M8
    and M9 are judged and M11 starts.
    """
    town = client.world(cid).get("town") or {}
    p = client.position(cid)
    if town.get("map_id") is None or p.get("map_id") != town["map_id"]:
        raise ValueError(f"character is on map {p.get('map_id')}, not the overworld {town.get('map_id')}")
    return int(town["map_id"]), (int(p["x"]), int(p["y"]))


def run_acceptance_smoke(
    client: Client,
    cfg: config.CharacterConfig,
    cid: int,
    metrics: AcceptanceHooks,
    *,
    timeout_s: float,
    out: Callable[[str], None] | None = None,
    prepare: Callable[[KnowledgeBase], None] | None = None,
    planner: Strategist | None = None,
    park_seconds: float = DEFAULT_PARK_SECONDS,
) -> tuple[float, KnowledgeBase]:
    """Run the runner with ``metrics`` until it stops or ``timeout_s`` elapses.

    ``prepare``, when given, runs on the loaded knowledge base before the
    runner starts (M9 clears earlier runs' entrance looks with it).
    ``planner`` is the AI planner (A35), from :func:`planner_for`.
    After the stop, the runner parks for up to ``park_seconds`` (A66);
    SIGINT or SIGTERM stops the run, and a second one cuts the park short.

    Returns the seconds played, the park phase not counted, and the knowledge
    base the runner wrote to; judge the run on that one, not a reload, which
    misses the run if the save failed.
    """
    stop, abort = threading.Event(), threading.Event()
    metrics.stop = stop
    started = time.monotonic()
    knowledge: KnowledgeBase = load_knowledge(cfg.world)
    if prepare is not None:
        prepare(knowledge)

    def emit(line: str) -> None:
        if out is not None:
            out(line)

    runner = Runner(
        cfg,
        metrics.wrap(client),
        cid,
        stop,
        emit,
        knowledge=knowledge,
        acceptance=metrics,
        strategist=planner,
        park_seconds=park_seconds,
        abort=abort,
    )

    def watchdog() -> None:
        if timeout_s <= 0:
            return
        if stop.wait(timeout_s):
            return
        emit(f"[{cfg.profile}] timeout after {timeout_s:.0f}s")
        stop.set()

    thread = threading.Thread(target=runner.run, daemon=True)
    wd = threading.Thread(target=watchdog, daemon=True)
    restore_signals = install_stop_signals(stop, abort, emit, park_seconds)
    try:
        thread.start()
        wd.start()
        while thread.is_alive() and not abort.is_set():
            thread.join(0.5)  # a bare join() would hold off the signal handlers
        thread.join(ABORT_JOIN_SECONDS)  # a second signal: give up on it after this
    finally:
        restore_signals()
    stop.set()
    elapsed = time.monotonic() - started
    if runner.park_report is not None:
        elapsed -= runner.park_report.seconds
    try:
        save_knowledge(knowledge)
    except OSError as e:
        emit(f"knowledge base {knowledge.world_code}: not saved: {e}")
    return elapsed, knowledge
