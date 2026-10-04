"""What the brain carries between windows besides the world model."""

from __future__ import annotations

from dataclasses import dataclass, field

from .navigation import NavSearchState
from .navigation.rejection import NavMemory
from .travel.ops import TravelOp
from .travel.strength import StrengthBracket
from .world import Pos


@dataclass
class Memory:
    """What the brain carries between windows besides the world model."""

    path: list[Pos] = field(default_factory=list)
    goal: str = ""
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
    travel_ops: list[TravelOp] = field(default_factory=list)  # parsed travel:* directives goals (A27)
    travel_index: int = 0
    strength: StrengthBracket = field(default_factory=StrengthBracket)
    loadout_key: tuple = ()  # reset strength bracket when armed/worn changes (A27)
    # Heal (A10): strategist buy ops; Shop (A21) consumes them later.
    buy_signals: list[dict] = field(default_factory=list)
    buy_signals_seen: set[tuple[str, str]] = field(default_factory=set)
    # Safe-zone regen sample: (start tick, start health, last tick seen). Only a
    # "yes" is saved to the knowledge base; a "no" holds for this run only.
    heal_regen_sample: tuple[int, int, int] | None = None
    heal_regen_absent: bool = False
    heal_wait: tuple[int, int] | None = None  # (tick, health) Heal began sending nothing, reset when health rises
    heal_backoff_until: int = -1  # Heal yields to Explore until this tick
    heal_tries: dict[tuple[str, int], int] = field(default_factory=dict)  # ("take"|"use", supply id) -> times sent
