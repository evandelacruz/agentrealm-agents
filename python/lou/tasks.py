"""Lou's task kinds: the strategist's vocabulary (docs/v3/SPEC.md **Tasks**).

Each kind is one small class. ``targets`` or ``actions`` says what works it
now, ``done`` and ``failed`` when it is over, ``progress`` a number that
rises as it nears done (flat too long and the executive fails it as
stalled). A task's own actions are keyed ``("task", ...)``, which is how
``wants`` knows them. A new behavior is a new class here, registered in
``KINDS`` and described in ``DOCS`` for the strategist.
"""

from __future__ import annotations

import time

from agentrealm_adk import supplies
from agentrealm_adk.world import Pos, chebyshev

from adk3.core.interfaces import Action
from adk3.core.plan import Step
from adk3.realm import actions as act
from adk3.realm.body import RealmBody
from adk3.realm.state import State

SAY_RANGE = 25  # cells: a Say to an NPC farther is refused (API rules § Speech)
READ_RANGE = 3  # cells: near enough that a sign is in sight in any light


class Task:
    """The defaults: never over on its own, no progress, no actions."""

    def __init__(self, step: Step, body: RealmBody):
        self.step, self.body, self.started = step, body, time.time()
        self.start_map: int | None = None

    def arg(self, name: str, default=None):
        return self.step.args.get(name, default)

    def cell(self) -> Pos | None:
        x, y = self.arg("x"), self.arg("y")
        return (int(x), int(y)) if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None

    def done(self, s: State) -> bool:
        return False

    def failed(self, s: State) -> str | None:
        return None

    def progress(self, s: State) -> float:
        return 0.0

    def actions(self, s: State) -> list[Action]:
        return []

    def wants(self, a: Action, s: State) -> float:
        return 1.0 if a.key[0] == "task" else 0.0

    def key(self, *rest) -> tuple:
        return ("task", str(self.step), *rest)

    def walk(self, s: State, targets: set[Pos], why: str, *, door: bool = False) -> list[Action]:
        a = act.walk(s, self.body, targets, self.key("walk"), why, door=door)
        return [a] if a is not None and a.kind == "walk" else []

    def walk_near(self, s: State, cell: Pos, reach: int, why: str) -> list[Action]:
        """A walk to the nearest open cell within ``reach`` of ``cell``."""
        x, y = cell
        near = {(x + dx, y + dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)}
        near = {p for p in near if s.w.walkable(p) or p not in s.w.view.tiles}
        return self.walk(s, near, why)

    def unreachable(self, s: State, targets: set[Pos]) -> bool:
        return bool(targets) and act.walk(s, self.body, targets, self.key("walk"), "") is None


class Explore(Task):
    """Walk to unseen ground, toward ``x, y`` when given, until ``find`` is in view or ``seconds`` pass."""

    def done(self, s: State) -> bool:
        find = self.arg("find")
        if find and seen(s, self.body, str(find)):
            return True
        return time.time() - self.started >= float(self.arg("seconds", 90))

    def frontier(self, s: State) -> set[Pos]:
        cells = s.w.frontier()
        toward = self.cell()
        if toward is None or not cells:
            return cells
        return set(sorted(cells, key=lambda p: chebyshev(p, toward))[:40])

    def failed(self, s: State) -> str | None:
        cells = self.frontier(s)
        if not cells:
            return "no unseen ground left in reach on this map"
        return "no way to any unseen ground" if self.unreachable(s, cells) else None

    def progress(self, s: State) -> float:
        return float(len(s.w.view.tiles))

    def actions(self, s: State) -> list[Action]:
        return self.walk(s, self.frontier(s), "explore")


class Goto(Task):
    """Walk to ``x, y`` on this map (onto it, or beside it when it cannot be stood on)."""

    def done(self, s: State) -> bool:
        c = self.cell()
        return c is not None and (s.pos == c or (chebyshev(s.pos, c) <= 1 and not s.w.walkable(c) and c in s.w.view.tiles))

    def failed(self, s: State) -> str | None:
        c = self.cell()
        if c is None:
            return "goto needs x and y"
        return f"no way to {c[0]},{c[1]} over the ground known" if self.unreachable(s, {c}) else None

    def progress(self, s: State) -> float:
        c = self.cell()
        return -float(chebyshev(s.pos, c)) if c else 0.0

    def actions(self, s: State) -> list[Action]:
        c = self.cell()
        if c is None:
            return []
        if c in s.w.view.tiles and not s.w.walkable(c):
            return self.walk_near(s, c, 1, f"to beside {c[0]},{c[1]}")
        return self.walk(s, {c}, f"to {c[0]},{c[1]}")


class Enter(Task):
    """Step through the door at ``x, y``: a level entrance or any other door. Done once we are on another map."""

    def done(self, s: State) -> bool:
        if self.start_map is None:
            self.start_map = s.map_id
        return s.map_id != self.start_map

    def failed(self, s: State) -> str | None:
        c = self.cell()
        if c is None:
            return "enter needs the door's x and y"
        rejected = [r for r in self.body.last_results if r.get("outcome") == "rejected"]
        for r in rejected:
            code = (r.get("rejection") or {}).get("code")
            if code in ("door_locked", "boss_room_occupied", "over_strength_ceiling"):
                return f"the door refused us: {code}"
        return f"no way to the door at {c[0]},{c[1]}" if self.unreachable(s, {c}) else None

    def progress(self, s: State) -> float:
        c = self.cell()
        return -float(chebyshev(s.pos, c)) if c else 0.0

    def actions(self, s: State) -> list[Action]:
        c = self.cell()
        a = act.walk(s, self.body, {c}, self.key("walk"), "through the door", door=True) if c else None
        return [a] if a is not None else []


class Get(Task):
    """Get a supply by code: walk onto it (a priced one is bought there). At ``x, y`` when given, else the nearest in view."""

    def item(self) -> str:
        return str(self.arg("item", ""))

    def have(self, s: State) -> int:
        return sum(1 for _, c in s.held if c == self.item()) + (self.item() in s.worn)

    def done(self, s: State) -> bool:
        if not hasattr(self, "baseline"):
            self.baseline = self.have(s)
        return self.have(s) > self.baseline or (supplies.eaten_on_pickup(self.item()) and not self.cells(s) and self.was_seen)

    def cells(self, s: State) -> set[Pos]:
        c = self.cell()
        found = {e.pos for e in s.supplies() if e.code == self.item()}
        if found:
            self.was_seen = True
        return {c} & found if c in found else found or ({c} if c else set())

    was_seen = False

    def failed(self, s: State) -> str | None:
        here = [e for e in s.supplies() if e.code == self.item()]
        price = min((e.gem_price or 0 for e in here), default=0)
        if here and price > s.gems:
            return f"{self.item()} costs {price} gems; we hold {s.gems}"
        cells = self.cells(s)
        if not cells:
            return f"no {self.item()} in view and no x, y given"
        return f"no way to the {self.item()}" if self.unreachable(s, cells) else None

    def progress(self, s: State) -> float:
        cells = self.cells(s)
        return -float(min((chebyshev(s.pos, c) for c in cells), default=0))

    def actions(self, s: State) -> list[Action]:
        cells = self.cells(s)
        out = self.walk(s, cells, f"get {self.item()}")
        out += [Action("take", self.key("take", e.id), f"take {e.code}", {"intents": [{"verb": "Take", "supply_id": e.id}],
                                                                          "at": s.pos})
                for e in s.supplies() if e.code == self.item() and chebyshev(e.pos, s.pos) <= 1]
        return out


class Equip(Task):
    """Arm a weapon or tool, or wear armor or an accessory, that we carry."""

    def item(self) -> str:
        return str(self.arg("item", ""))

    def done(self, s: State) -> bool:
        return s.armed == self.item() or self.item() in s.worn

    def failed(self, s: State) -> str | None:
        return None if any(c == self.item() for _, c in s.held) else f"we do not carry {self.item()}"

    def actions(self, s: State) -> list[Action]:
        sid = next((i for i, c in s.held if c == self.item()), None)
        if sid is None:
            return []
        verb = "Wear" if supplies.worn_slot(self.item()) else "Arm"
        return [Action("equip", self.key(verb), f"{verb.lower()} {self.item()}", {"intents": [{"verb": verb, "supply_id": sid}],
                                                                                   "at": s.pos})]


class Fight(Task):
    """Fight the NPC ``id``, or the nearest of type ``npc``, until it is dead or gone from view."""

    def target(self, s: State):
        want_id, want_code = self.arg("id"), self.arg("npc")
        cands = [e for e in s.npcs() if (want_id is not None and e.id == int(want_id)) or (want_code and e.code == want_code)]
        return min(cands, key=lambda e: chebyshev(e.pos, s.pos), default=None)

    def done(self, s: State) -> bool:
        t = self.target(s)
        if t is not None:
            self.seen_it = True
        return t is None and getattr(self, "seen_it", False)

    def failed(self, s: State) -> str | None:
        if self.target(s) is None and not getattr(self, "seen_it", False):
            return "no such NPC in view"
        return None

    def progress(self, s: State) -> float:
        t = self.target(s)
        if t is None:
            return 0.0
        hurt = -(t.health or 0) if t.health is not None else 0
        return hurt - chebyshev(t.pos, s.pos)

    def actions(self, s: State) -> list[Action]:
        t = self.target(s)
        if t is None:
            return []
        out = []
        swing = act.swing(s, self.body, t)
        if swing is not None:
            swing.key = self.key("attack", t.id)
            out.append(swing)
        elif chebyshev(t.pos, s.pos) > (s.w.attack_range or 1):
            out += self.walk_near(s, t.pos, s.w.attack_range or 1, f"close on {t.code} {t.id}")
        return out


class Read(Task):
    """Read the sign at ``x, y``."""

    def done(self, s: State) -> bool:
        c = self.cell()
        return any(r.get("text") for r in self.body.last_results) or (c is not None and c in getattr(self, "read_at", ()))

    def failed(self, s: State) -> str | None:
        return None if self.cell() else "read needs the sign's x and y"

    def progress(self, s: State) -> float:
        c = self.cell()
        return -float(chebyshev(s.pos, c)) if c else 0.0

    def actions(self, s: State) -> list[Action]:
        c = self.cell()
        if c is None:
            return []
        if chebyshev(c, s.pos) > READ_RANGE:
            return self.walk_near(s, c, READ_RANGE, "to the sign")
        return [Action("read", self.key("read"), "read the sign",
                       {"intents": [{"verb": "Read", "target": {"kind": "block", "x": c[0], "y": c[1]}}], "at": s.pos})]


class Talk(Task):
    """Say hello to the NPC ``id`` (or the nearest of type ``npc``) and hear its line."""

    def target(self, s: State):
        want_id, want_code = self.arg("id"), self.arg("npc")
        cands = [e for e in s.npcs() if (want_id is not None and e.id == int(want_id)) or (want_code and e.code == want_code)]
        return min(cands, key=lambda e: chebyshev(e.pos, s.pos), default=None)

    def done(self, s: State) -> bool:
        return any(ev.get("kind") == "SpokenTo" for ev in self.body.last_events)

    def failed(self, s: State) -> str | None:
        return None if self.target(s) is not None else "no such NPC in view"

    def progress(self, s: State) -> float:
        t = self.target(s)
        return -float(chebyshev(t.pos, s.pos)) if t else 0.0

    def actions(self, s: State) -> list[Action]:
        t = self.target(s)
        if t is None:
            return []
        if chebyshev(t.pos, s.pos) > SAY_RANGE // 2:
            return self.walk_near(s, t.pos, 2, f"to {t.code}")
        return [Action("say", self.key("say", t.id), f"hello to {t.code}",
                       {"intents": [{"verb": "Say", "npc_id": t.id, "text": "hello"}], "at": s.pos})]


class Use(Task):
    """Use what is armed (or ``item``, armed first) on the block at ``x, y``: break it, cut it, open it. Done when the block changes."""

    def done(self, s: State) -> bool:
        c = self.cell()
        if c is None:
            return False
        now = s.w.view.tiles.get(c)
        if not hasattr(self, "was"):
            self.was = now
        return now != self.was

    def failed(self, s: State) -> str | None:
        item = self.arg("item")
        if item and not any(c == item for _, c in s.held) and s.armed != item:
            return f"we do not carry {item}"
        return None if self.cell() else "use needs x and y"

    def progress(self, s: State) -> float:
        c = self.cell()
        return -float(chebyshev(s.pos, c)) if c else 0.0

    def actions(self, s: State) -> list[Action]:
        c, item = self.cell(), self.arg("item")
        if c is None:
            return []
        if chebyshev(c, s.pos) > 1:
            return self.walk_near(s, c, 1, "to the block")
        intents = []
        if item and s.armed != item:
            sid = next((i for i, code in s.held if code == item), None)
            if sid is not None:
                intents.append({"verb": "Arm", "supply_id": sid})
        intents.append({"verb": "Use", "target": {"kind": "block", "x": c[0], "y": c[1]}})
        return [Action("use", self.key("use"), f"use {item or s.armed} on {c[0]},{c[1]}", {"intents": intents, "at": s.pos})]


class Gather(Task):
    """Pick up free gems in view, exploring for more, until we hold ``gems``."""

    def done(self, s: State) -> bool:
        return s.gems >= int(self.arg("gems", 0))

    def piles(self, s: State) -> set[Pos]:
        return {e.pos for e in s.supplies() if not e.gem_price and supplies.row(e.code) is not None
                and supplies.row(e.code).supply_class == "gem"}

    def progress(self, s: State) -> float:
        return float(s.gems) + len(s.w.view.tiles) / 1000.0

    def actions(self, s: State) -> list[Action]:
        piles = self.piles(s)
        return self.walk(s, piles, "to gems") if piles else self.walk(s, s.w.frontier(), "look for gems")


def seen(s: State, body: RealmBody, find: str) -> bool:
    """``find`` is in view: ``door``, ``sign``, ``entrance``, ``npc:<type>`` or ``supply:<code>``."""
    kind, _, code = find.partition(":")
    if kind == "door":
        return bool(s.doors())
    if kind == "sign":
        return bool(s.signs())
    if kind == "entrance":
        return any(p in s.w.view.tiles for p in body.entrances.get(s.map_id, []))
    if kind == "npc":
        return any(e.code == code for e in s.npcs())
    if kind == "supply":
        return any(e.code == code for e in s.supplies())
    return False


KINDS: dict[str, type[Task]] = {
    "explore": Explore, "goto": Goto, "enter": Enter, "get": Get, "equip": Equip,
    "fight": Fight, "read": Read, "talk": Talk, "use": Use, "gather": Gather,
}

DOCS = """\
explore {x?, y?, find?, seconds?}: walk to unseen ground, toward x, y when given; done when `find` is in view
  (door | sign | entrance | npc:<type> | supply:<code>) or after `seconds` (default 90).
goto {x, y}: walk to a cell on this map.
enter {x, y}: step through the door at x, y (a level entrance, a framed_door or rock_entry); done on the new map.
get {item, x?, y?}: walk onto a supply of that code and take it; a priced one is bought there with gems.
equip {item}: arm a carried weapon or tool, or wear carried armor or an accessory.
fight {id? | npc?}: fight that NPC (by id, or the nearest of that type) until it is dead or out of view.
read {x, y}: read the sign at x, y.
talk {id? | npc?}: say hello to a helper and hear its line.
use {x, y, item?}: use what is armed (or `item`, armed first) on the block at x, y: break a wall, cut grass, open a chest block.
gather {gems}: pick up free gems in view, exploring for more, until we hold that many.
"""


def build(body: RealmBody):
    def make(step: Step) -> Task:
        return KINDS[step.kind](step, body)
    return make
