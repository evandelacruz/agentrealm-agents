"""What the brain carries between windows besides the world model."""

from __future__ import annotations

from dataclasses import dataclass, field

from .navigation import NavSearchState
from .navigation.rejection import NavMemory
from .navigation.stuck import NavStuckMemory
from .navigation.walk import Walk
from .travel.strength import StrengthBracket
from .world import Pos

SIGNALS_KEPT = 16  # newest strategist signals kept until the strategist drains them (A35)

@dataclass(frozen=True)
class BossFight:
    """A boss seen while its ``fight_boss`` op is current (A38)."""

    op: dict  # the plan op this fight belongs to; a pop, drop or reload ends it
    boss_id: int  # the boss NPC's id
    since_tick: int  # tick the boss was first seen


@dataclass
class HuntSearch:
    """Travel's search for a hunting ground (A27): the ``travel`` op it runs
    for, the tick it began, the tick Travel last worked on it, the tick
    until which spare windows read zones around the character for one, and
    how many of those reads it has spent (``zone_discovery.hunt_probe``)."""

    op: dict
    since: int
    last: int
    probe_until: int
    probes: int = 0


@dataclass
class Memory:
    """What the brain carries between windows besides the world model."""

    path: list[Pos] = field(default_factory=list)
    goal: str = ""
    goal_op: dict | None = None  # the plan op m.path was set for, so a same-kind head swap replans (A34)
    walks: dict[str, Walk] = field(default_factory=dict)  # goal -> the path its walk committed to, kept while it stays the best way (A15)
    state: str = ""  # active state (A5): kept until its done() holds or a higher guard fires
    need_position: bool = True
    need_self: bool = True
    windows_since_self: int = 0
    pending: dict | None = None  # last non-queue intent submitted, awaiting its result
    pending_queue: str | None = None  # the queue_id movement or intent was sent under
    pending_intents: list[dict] | None = None  # full queue last submitted with intents
    pending_next_index: int = 0  # next intent index not yet known to have run (results and queue.next_index)
    held_queue: dict | None = None  # server queue {"queue_id", "next_index"} while not empty
    queue_sent_tick: int = 0  # tick the last multi-intent queue was answered at
    cancel_queue: bool = False  # send [] next tick: the held queue was planned from a stale position
    last_step_tick: int | None = None  # tick our last Step applied, to pace the next queue
    last_use_tick: int | None = None  # tick our last Use applied (weapon cooldown, A1)
    last_speech_tick: int | None = None  # tick our last Say/Broadcast applied (A1)
    nav: NavMemory = field(default_factory=NavMemory)  # what Step rejections taught the map (A14)
    nav_stuck: NavStuckMemory = field(default_factory=NavStuckMemory)  # stuck detection and escalation (A15)
    alarm: bool = False  # Damaged or Attacked since the last entity read
    last_poll_tick: int = -1  # sim tick of the last POST tick (M6 cadence)
    calm_poll_interval: int = 7  # ticks between calm polls, 4–10 after each poll
    queued_ticks: int = 0  # intents still queued after the last poll, one tick each
    hurt_last_poll: bool = False  # the last poll's events carried Damaged
    resend_held_queue: bool = False  # replace the held walk queue on the next poll (A43)
    path_blockers: set = field(default_factory=set)  # blocked cells the walk queue already crossed when sent (A43)
    path_threats: set = field(default_factory=set)  # hostiles (kind, id) whose reach the walk queue already crossed when sent (A63)
    walk_skip: set = field(default_factory=set)  # hostiles the walk being decided was planned without (Retreat sets it, A63)
    path_skip: set = field(default_factory=set)  # walk_skip of the walk queue sent, never counted in path_threats (A63)
    planned_threats: set = field(default_factory=set)  # hostiles whose reach Heal's or Retreat's path crossed when planned (A63)
    safe_unreachable: dict = field(default_factory=dict)  # (map_id, cell) -> tick a path check found no way to that safe cell (``pathing.reachable_safe_goal``)
    safe_threatened: dict = field(default_factory=dict)  # (map_id, cell) -> {(kind, id): tick that hostile last had the safe cell in its reach} (``pathing.reachable_safe_goal``)
    zone_probe: tuple[int, Pos] | None = None  # cell choose_call picked for this window's zone read (A7)
    hunt_search: HuntSearch | None = None  # Travel's search for a hunting ground when none is known (A27)
    warp_from: tuple[int, Pos, str] | None = None  # door stepped onto, awaiting position read (A26)
    goto_reached: tuple[int | None, Pos] | None = None  # (policy.goto_map, cell) of the policy goto once stood on: satisfied, not owed again (A16)
    investigate_rejections: dict[str, int] = field(default_factory=dict)  # Read/Say key -> refused count (A30)
    greetings: dict[int, tuple[int, int | None]] = field(default_factory=dict)  # npc id -> (greetings sent, tick of the last), Greet (A65)
    greet_say_npc: int | None = None  # npc id of the last Say submitted when Greet decided it; None after any other Say (A65)
    corridors: dict[str, NavSearchState] = field(default_factory=dict)  # plan ("chest", "goto") -> its corridor search, resumed across replans (A13)
    gather_target: tuple[str, Pos] | None = None  # ("pile" | "bush" | "grass" | "out" | "off", cell) Gather is walking toward (A22)
    gather_status: str = ""  # Gather's last decision, e.g. cutting, walking to grass, blocked by hostile (planner State)
    # Gather's stall clock: (tick this spell of Gather began, tick of its latest decision) (A63 run 3).
    gather_spell: tuple[int, int] | None = None
    # A hostile near Gather that has not hit us: (its id, tick its shadow clock
    # started, tick Gather last saw it near) (A63 run 3).
    gather_shadow: tuple[int, int, int] | None = None
    flee_path: list[Pos] = field(default_factory=list)  # Flee's committed escape, kept until it arrives, is blocked or Flee stops (A9, A58)
    # Cells the escape the oscillation guard forced keeps off: the ones it paced on (A15).
    # The planned flee_path already excludes them; this only keeps them shut for
    # _committed_step's open check and its best-step comparison while that escape runs.
    flee_avoid: set[Pos] = field(default_factory=set)
    # Whether fleeing works (A9, A58 run 9): (tick, gap to the nearest hostile)
    # at each Flee decision, the tick Flee began, and whether it gave up running.
    flee_gaps: list[tuple[int, int]] = field(default_factory=list)
    flee_since: int = 0
    flee_failed: bool = False
    # Retreat (A9): the goal of the walk queue it last sent, so a reflex probe
    # lets that queue run instead of replacing it; and (tick, distance to the
    # goal) at each Retreat decision toward ``retreat_to``, to see it losing ground.
    retreat_walk: Pos | None = None
    retreat_to: Pos | None = None
    retreat_gaps: list[tuple[int, int]] = field(default_factory=list)
    # The runner's park phase (A66): the run is over and only the survival
    # reflexes and Park run, walking to safe ground before the exit.
    parking: bool = False
    strength: StrengthBracket = field(default_factory=StrengthBracket)
    loadout_key: tuple = ()  # reset strength bracket when armed/worn changes (A27)
    # Clues (A32): {"trigger": "clue", **kb.clues row} per new clue; the strategist (A35) drains them.
    clue_signals: list[dict] = field(default_factory=list)
    # Strategist (A35): death, goal_done and goal_failed triggers, via queue_signal; clue and stuck live elsewhere.
    strategist_signals: list[dict] = field(default_factory=list)
    last_decision: str = ""  # "State: reason" of the last decision, for the planner's stall line
    strategist_progress_tick: int = -1  # last tick with an applied Step (the idle trigger counts from it); -1 before the first real tick
    # Shop (A21): (supply id, code, gems before, supply pos, map id, tick sent)
    # of the Take in flight. Settled on an applied Take or a gem drop, counted
    # as a refusal on a rejection; it expires on leaving the shop cell or a timeout.
    shop_pending: tuple[int, str, int | None, tuple[int, int], int | None, int] | None = None
    # Shop (A21): supply id -> rejected Take count; cleared when shop_refusal_key
    # (loadout, gems, map) changes, since any of those can turn a refusal around.
    shop_refusals: dict[int, int] = field(default_factory=dict)
    shop_refusal_key: tuple = ()
    # Safe-zone regen sample: (start tick, start health, last tick seen). Only a
    # "yes" is saved to the knowledge base; a "no" holds for this run only.
    heal_regen_sample: tuple[int, int, int] | None = None
    heal_regen_absent: bool = False
    heal_supplies_asked: bool = False  # Heal raised `heal_supplies` for the planner this hurt spell
    heal_tries: dict[tuple[str, int], int] = field(default_factory=dict)  # ("take"|"use", supply id) -> times sent
    heal_rearm: str | None = None  # weapon code armed before a drink; restored once, on Heal's next decision (A24)
    heal_pending: tuple[int, str, str] | None = None  # (health before, supply code, "take"|"use") awaiting observation
    # Boss (A38): the fight under way, or None. Set and cleared only by states/boss.sync_boss.
    boss: BossFight | None = None
    solve_rearm: str | None = None  # code armed before Solve armed a use_block supply, re-armed once no solve op is on top (A39)
    break_rearm: str | None = None  # code armed before Break, restored when break finishes (A28)
    break_pending: tuple[int, Pos, str] | None = None  # map, block and capability a Use in flight targets (A28)
    equip_refused: set[tuple[str | None, str]] = field(default_factory=set)  # (subtype, slot) Equip was refused; (None, slot) for Remove (A19)
    equip_refused_sig: tuple | None = None  # loadout and inventory the refusals hold for; None until the next observation syncs it (A19)
    equip_not_wearable: set[str] = field(default_factory=set)  # subtypes Wear rejected with not_wearable for the run (A55)
    equip_try_refused: set[str] = field(default_factory=set)  # subtypes whose slot-learn Wear was refused transiently (A55)


def queue_signal(m: Memory, payload: dict) -> None:
    """Queue one strategist trigger (A35); keep the newest ``SIGNALS_KEPT``."""
    m.strategist_signals.append(payload)
    del m.strategist_signals[:-SIGNALS_KEPT]


def note_goal_done(m: Memory | None, op: dict | None, reason: str) -> None:
    """Queue a ``goal_done`` trigger when a plan op finishes."""
    if m is not None and op is not None:
        queue_signal(m, {"trigger": "goal_done", "op": dict(op), "reason": reason})


def note_goal_failed(m: Memory | None, op: dict | None, reason: str) -> None:
    """Queue a ``goal_failed`` trigger when a plan op is dropped."""
    if m is not None and op is not None:
        queue_signal(m, {"trigger": "goal_failed", "op": dict(op), "reason": reason})
