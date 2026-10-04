"""What the brain carries between windows besides the world model."""

from __future__ import annotations

from dataclasses import dataclass, field

from .navigation import NavSearchState
from .navigation.rejection import NavMemory
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
    curiosity_segments: list[tuple[int, int]] = field(default_factory=list)  # (start_tick, length) of detour queues charged to the curiosity budget (A30)
    investigate_rejections: dict[str, int] = field(default_factory=dict)  # interest item key -> refused Read/Say count (A30)
