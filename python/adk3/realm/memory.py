"""Memory: what the character has learned, kept per world across runs (docs/v3/SPEC.md **Memory**).

The game serves only what is in view (Manual §3 Perception), so everything
worth knowing later is written here as it is seen or heard: every sign,
scroll and helper line, every level entrance and what happened at it,
where things are sold, what hostiles hit for, where gems drop, every death.
Tiles and door warps live in the ADK's world knowledge base beside it.

``revision`` rises with every new fact. ``happened`` and ``news`` hold this
run's reaction outcomes for the strategist; they are not kept.

Clues are world secrets: this file lives under ``python/.state/``, which is
never committed (docs/GAME_NOTES.md **No spoilers in this repo**).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

Pos = tuple[int, int]

REGION = 16  # blocks per side of a region, the unit gem yields are kept in

HAPPENED_KEPT = 15  # reaction outcomes kept for the strategist


def place_key(map_id: int, pos: Pos) -> str:
    return f"{map_id}:{pos[0]},{pos[1]}"


def region_key(map_id: int, pos: Pos) -> str:
    return f"{map_id}:{pos[0] // REGION},{pos[1] // REGION}"


@dataclass
class Clue:
    """A line of text the world gave us."""

    text: str
    source: str  # sign | scroll | helper
    speaker: str  # the helper's type, or "" for a sign or scroll
    map_id: int
    x: int
    y: int
    at: float = 0.0


@dataclass
class Entrance:
    """A level entrance the minimap marks (Manual §5.3 minimap: every one, found or not, unnumbered)."""

    map_id: int
    x: int
    y: int
    block: str = ""  # what stands on the mark when seen: a door, or a block hiding one
    locked: bool = False
    reached: bool = False  # we stood next to it
    refused: str = ""  # the last rejection stepping onto it: door_locked, boss_room_occupied, ...
    leads_to: int | None = None  # the map it opened onto
    level: int | None = None  # that map's level number, from the minimap


@dataclass
class Sighting:
    """A priced supply seen on the ground: a shop's stock (Manual §11 Shops)."""

    code: str
    price: int
    map_id: int
    x: int
    y: int
    at: float = 0.0


@dataclass
class Helper:
    """An NPC we said hello to, and whether it answered (Manual §11 Speech)."""

    code: str
    map_id: int
    x: int
    y: int
    answered: bool = False


@dataclass
class Attempt:
    """One boss fight and how it ended."""

    at: float
    result: str  # cleared | died | left | timed_out
    seconds: float
    boss_health_left: int | None = None


@dataclass
class Level:
    """What we know of one level."""

    number: int
    entrance: str = ""  # place key of the entrance that opens it
    maps: list[int] = field(default_factory=list)
    boss: str = ""  # the boss's type
    boss_max_health: int | None = None
    boss_door: str = ""  # place key of the door into the boss room
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class Yield:
    """Our own cuts and breaks in one region, and the gems they dropped."""

    breaks: int = 0
    gems: int = 0


@dataclass
class Death:
    at: float
    map_id: int
    x: int
    y: int
    killer: str = ""  # the type that landed the last hit
    chest_id: int | None = None
    chest_map: int | None = None
    chest_x: int | None = None
    chest_y: int | None = None
    items: list[str] = field(default_factory=list)  # what the chest holds: everything carried but gems and the starting kit


@dataclass
class Memory:
    path: Path | None = None
    clues: list[Clue] = field(default_factory=list)
    signs_read: list[str] = field(default_factory=list)  # place keys
    helpers: dict[str, Helper] = field(default_factory=dict)  # place key -> helper
    entrances: dict[str, Entrance] = field(default_factory=dict)  # place key -> entrance
    levels: dict[int, Level] = field(default_factory=dict)
    shops: dict[str, Sighting] = field(default_factory=dict)  # place key -> what was for sale there
    yields: dict[str, Yield] = field(default_factory=dict)  # region key -> our breaks there
    kills: dict[str, Yield] = field(default_factory=dict)  # hostile type -> our kills of it and the gems they dropped
    no_effect: list[str] = field(default_factory=list)  # place keys a Use on changed nothing
    territory: dict[str, list[str]] = field(default_factory=dict)  # hostile type -> the block types it keeps to
    deaths: list[Death] = field(default_factory=list)
    revision: int = 0
    happened: list[str] = field(default_factory=list)  # this run: what the reactions did and why, newest last
    news: list[str] = field(default_factory=list)  # this run: what happened that the strategist has not heard yet

    # Learning.

    def learned(self) -> None:
        self.revision += 1

    def note(self, text: str, news: bool = True) -> None:
        """What just happened (a reaction's outcome), for the strategist to read; ``news`` when it should hear at once."""
        self.happened = (self.happened + [text])[-HAPPENED_KEPT:]
        if news:
            self.news.append(text)

    def take_news(self) -> list[str]:
        out, self.news = self.news, []
        return out

    def add_clue(self, clue: Clue) -> bool:
        if any(c.text == clue.text and c.speaker == clue.speaker for c in self.clues):
            return False
        clue.at = clue.at or time.time()
        self.clues.append(clue)
        self.learned()
        return True

    def read_sign(self, map_id: int, pos: Pos) -> None:
        key = place_key(map_id, pos)
        if key not in self.signs_read:
            self.signs_read.append(key)

    def greeted(self, code: str, map_id: int, pos: Pos, answered: bool) -> None:
        key = place_key(map_id, pos)
        known = self.helpers.get(key)
        if known is None or (answered and not known.answered):
            self.helpers[key] = Helper(code, map_id, pos[0], pos[1], answered)
            self.learned()

    def entrance(self, map_id: int, pos: Pos) -> Entrance:
        key = place_key(map_id, pos)
        if key not in self.entrances:
            self.entrances[key] = Entrance(map_id, pos[0], pos[1])
            self.learned()
        return self.entrances[key]

    def level(self, number: int) -> Level:
        if number not in self.levels:
            self.levels[number] = Level(number)
            self.learned()
        return self.levels[number]

    def saw_for_sale(self, code: str, price: int, map_id: int, pos: Pos) -> None:
        key = place_key(map_id, pos)
        known = self.shops.get(key)
        if known is None or known.code != code or known.price != price:
            self.learned()
        self.shops[key] = Sighting(code, price, map_id, pos[0], pos[1], time.time())

    def broke(self, map_id: int, pos: Pos, gems: int = 0) -> None:
        y = self.yields.setdefault(region_key(map_id, pos), Yield())
        y.breaks += 1
        y.gems += gems

    def dropped_gems(self, map_id: int, pos: Pos, gems: int) -> None:
        self.yields.setdefault(region_key(map_id, pos), Yield()).gems += gems

    def killed(self, code: str, gems: int = 0) -> None:
        k = self.kills.setdefault(code, Yield())
        k.breaks += 1
        k.gems += gems

    def kill_dropped(self, code: str, gems: int) -> None:
        self.kills.setdefault(code, Yield()).gems += gems

    def gems_per_kill(self, code: str, prior: float, weight: int = 5) -> float:
        """What a kill of ``code`` drops on average: our kills, with ``prior`` counted as ``weight`` kills."""
        k = self.kills.get(code, Yield())
        return (k.gems + prior * weight) / (k.breaks + weight)

    def changed_nothing(self, map_id: int, pos: Pos) -> None:
        key = place_key(map_id, pos)
        if key not in self.no_effect:
            self.no_effect.append(key)

    def keeps_to(self, code: str, ground: set[str]) -> None:
        """A hostile of ``code`` stopped at the edge of ``ground``: it keeps to its zone (Manual §11 Hostiles)."""
        known = set(self.territory.get(code, []))
        if not ground <= known:
            self.territory[code] = sorted(known | ground)
            self.learned()

    def died(self, death: Death) -> None:
        self.deaths = (self.deaths + [death])[-20:]
        self.learned()

    # Reading.

    def sellers(self, code: str) -> list[Sighting]:
        return [s for s in self.shops.values() if s.code == code]

    def heard(self, map_id: int, pos: Pos) -> bool:
        return place_key(map_id, pos) in self.helpers

    def was_read(self, map_id: int, pos: Pos) -> bool:
        return place_key(map_id, pos) in self.signs_read

    def gems_per_break(self, map_id: int, pos: Pos) -> float | None:
        """Gems our breaks dropped per break in this region, or None before any break there."""
        y = self.yields.get(region_key(map_id, pos))
        return y.gems / y.breaks if y and y.breaks else None

    def last_death(self) -> Death | None:
        return self.deaths[-1] if self.deaths else None

    # Keeping.

    @classmethod
    def load(cls, path: Path | None) -> Memory:
        m = cls(path)
        if path is None or not path.exists():
            return m
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return m
        m.clues = [Clue(**c) for c in raw.get("clues", [])]
        m.signs_read = list(raw.get("signs_read", []))
        m.helpers = {k: Helper(**v) for k, v in raw.get("helpers", {}).items()}
        m.entrances = {k: Entrance(**v) for k, v in raw.get("entrances", {}).items()}
        m.levels = {int(k): _level(v) for k, v in raw.get("levels", {}).items()}
        m.shops = {k: Sighting(**v) for k, v in raw.get("shops", {}).items()}
        m.yields = {k: Yield(**v) for k, v in raw.get("yields", {}).items()}
        m.kills = {k: Yield(**v) for k, v in raw.get("kills", {}).items()}
        m.no_effect = list(raw.get("no_effect", []))
        m.territory = {k: list(v) for k, v in raw.get("territory", {}).items()}
        m.deaths = [Death(**d) for d in raw.get("deaths", [])]
        return m

    def save(self) -> None:
        if self.path is None:
            return
        raw = {k: v for k, v in asdict(self).items() if k not in ("path", "revision", "happened", "news")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=1, sort_keys=True))


def _level(raw: dict) -> Level:
    attempts = [Attempt(**a) for a in raw.pop("attempts", [])]
    return Level(**raw, attempts=attempts)
