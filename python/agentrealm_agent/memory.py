"""What the brain carries between windows besides the world model."""

from __future__ import annotations

from dataclasses import dataclass, field

from .navigation import NavSearchState
from .navigation.rejection import NavMemory
from .navigation.stuck import NavStuckMemory
from .travel.ops import TravelOp
from .travel.strength import StrengthBracket
from .world import Pos


@dataclass(frozen=True)
class BossFight:
    """A boss seen while its ``fight_boss`` op is current (A38)."""

    op: dict  # the plan op this fight belongs to; a pop, drop or reload ends it
    boss_id: int  # the boss NPC's id
    since_tick: int  # tick the boss was first seen


@dataclass
class Memory:
    """What the brain carries between windows besides the world model."""

    path: list[Pos] = field(default_factory=list)
    goal: str = ""
    goal_op: dict | None = None  # the plan op m.path was set for, so a same-kind head swap replans (A34)
    state: str = ""  # active state (A5): kept until its done() holds or a higher guard fires
    need_position: bool = True
    need_self: bool = True
    windows_since_self: int = 0
    pending: dict | None = None  # last non-queue intent submitted, awaiting its result
    pending_queue: str | None = None  # the queue_id movement or intent was sent under
    pending_intents: list[dict] | None = None  # full queue last submitted with intents
    pending_next_index: int = 0  # next intent index still awaiting a result
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
    zone_probe: tuple[int, Pos] | None = None  # cell choose_call picked for this window's zone read (A7)
    warp_from: tuple[int, Pos, str] | None = None  # door stepped onto, awaiting position read (A26)
    corridors: dict[str, NavSearchState] = field(default_factory=dict)  # plan ("chest", "goto") -> its corridor search, resumed across replans (A13)
    investigate_rejections: dict[str, int] = field(default_factory=dict)  # interest item key -> refused Read/Say count (A30)
    curiosity_spans: list[tuple[int, int]] = field(default_factory=list)  # (start tick, length) charged Investigate/Break queues (A30)
    gather_target: tuple[str, Pos] | None = None  # ("pile" | "bush" | "grass", cell) Gather is walking toward (A22)
    gather_backoff_until: int = -1  # Gather yields to Explore until this tick (A22)
    travel_ops: list[TravelOp] = field(default_factory=list)  # parsed travel:* directives goals (A27)
    travel_index: int = 0
    strength: StrengthBracket = field(default_factory=StrengthBracket)
    loadout_key: tuple = ()  # reset strength bracket when armed/worn changes (A27)
    # Heal (A10): strategist buy ops; Shop (A21) consumes them later.
    buy_signals: list[dict] = field(default_factory=list)
    buy_signals_seen: set[tuple[str, str]] = field(default_factory=set)
    # Clues (A32): {"trigger": "clue", **kb.clues row} per new clue; the strategist (A35) drains them.
    clue_signals: list[dict] = field(default_factory=list)
    # Shop (A21): (supply id, code, gems before, supply pos, map id, tick sent)
    # of the Take in flight. Its buy signal is consumed on an applied Take or a
    # gem drop, kept on a rejection; it expires on leaving the shop cell or a timeout.
    shop_pending: tuple[int, str, int | None, tuple[int, int], int | None, int] | None = None
    # Shop (A21): supply id -> rejected Take count; cleared when shop_refusal_key
    # (loadout, gems, map) changes, since any of those can turn a refusal around.
    shop_refusals: dict[int, int] = field(default_factory=dict)
    shop_refusal_key: tuple = ()
    # Safe-zone regen sample: (start tick, start health, last tick seen). Only a
    # "yes" is saved to the knowledge base; a "no" holds for this run only.
    heal_regen_sample: tuple[int, int, int] | None = None
    heal_regen_absent: bool = False
    heal_wait: tuple[int, int] | None = None  # (tick, health) Heal began sending nothing, reset when health rises
    heal_backoff_until: int = -1  # Heal yields to Explore until this tick
    heal_tries: dict[tuple[str, int], int] = field(default_factory=dict)  # ("take"|"use", supply id) -> times sent
    heal_rearm: str | None = None  # weapon code armed before a drink; restored once (A24)
    heal_pending: tuple[int, str, str] | None = None  # (health before, supply code, "take"|"use") awaiting observation
    # Boss (A38): the fight under way, or None. Set and cleared only by states/boss.sync_boss.
    boss: BossFight | None = None
    solve_rearm: str | None = None  # code armed before Solve armed a use_block supply, re-armed once no solve op is on top (A39)
    break_rearm: str | None = None  # code armed before Break, restored when break finishes (A28)
    break_pending: tuple[int, Pos, str] | None = None  # map, block and capability a Use in flight targets (A28)
    break_odd: tuple[int, Pos] | None = None  # map and odd block Break is walking toward (A31)
    break_odd_refusals: dict[tuple[int, Pos], int] = field(default_factory=dict)  # (map, odd block) -> times Break found no route to it (A31)
    break_odd_pick: tuple | None = None  # (inputs, choice): the odd pick cached for one decision window (A31)
    equip_refused: set[tuple[str | None, str]] = field(default_factory=set)  # (subtype, slot) Equip was refused; (None, slot) for Remove (A19)
    equip_refused_sig: tuple | None = None  # loadout and inventory the refusals hold for; None until the next observation syncs it (A19)
