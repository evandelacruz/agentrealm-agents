"""New information that wakes the planner at once (A71).

The strategist replans on events and on a timer. A find that changes what
the plan should be is an event too, so it raises one ``discovery`` trigger
instead of waiting up to ``replan_s`` for the timer:

- an NPC seen for the first time this run that is not known hostile;
- a level entrance the knowledge base did not hold before;
- an item seen for sale that the gems held can now buy;
- a pack of known-hostile NPCs in a part of the map (a ``PACK_CELL`` square)
  where none was seen before;
- a sign or statue (a readable cell, ``MapView.readable``) seen for the first
  time that the knowledge base has no read of (``cell_was_read``).

What was known when the run started primes the record and raises nothing.
Each find is raised once a run (an item again only after the gems held
fell below its price), so an NPC walking in and out of view does not raise
it again. The strategist merges the finds of one window into one trigger
and lets a discovery alone start a call at most every
``DISCOVERY_GAP_S`` (``Strategist.on_window``), so one find never sets off
a burst of replans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .investigation import cell_was_read
from .knowledge_base import KnowledgeBase, knowledge_items
from .survival import known_hostile
from .travel.knowledge import iter_entrances
from .world import Pos, WorldModel

# A discovery with no other trigger waits this long after the last call.
DISCOVERY_GAP_S = 5.0
# Known hostiles inside one square of this side are one pack.
PACK_CELL = 16


def seen_prices(w: WorldModel, knowledge: KnowledgeBase | None) -> dict[str, int]:
    """The gem price of every item seen for sale: ``items`` rows, then priced supplies in sight."""
    prices: dict[str, int] = {}
    for code, row in knowledge_items(knowledge).items():
        price = row.get("gem_price") if isinstance(row, dict) else None
        if isinstance(price, int) and price > 0:
            prices[code] = price
    for e in w.entities:
        if e.kind == "supply" and e.code and isinstance(e.gem_price, int) and e.gem_price > 0:
            prices[e.code] = e.gem_price
    return prices


@dataclass
class Discoveries:
    """What this run has already seen, so only new finds raise a trigger."""

    npcs: set[int] = field(default_factory=set)
    entrances: set[tuple[int, Pos]] = field(default_factory=set)
    affordable: set[str] = field(default_factory=set)
    packs: set[tuple[int | None, int, int]] = field(default_factory=set)
    signs: set[tuple[int, Pos]] = field(default_factory=set)
    primed: bool = False

    def scan(self, w: WorldModel, knowledge: KnowledgeBase | None) -> list[dict[str, Any]]:
        """The finds since the last scan, one row each; none on the first scan."""
        if w.pos is None:
            return []
        npcs = {e.id: e for e in w.entities if e.kind == "npc" and not known_hostile(w, e)}
        entrances = {(mid, pos) for mid, pos, _ in iter_entrances(knowledge)}
        gems = w.gems or 0
        affordable = {code: price for code, price in seen_prices(w, knowledge).items() if price <= gems}
        packs: dict[tuple[int | None, int, int], list] = {}
        for e in w.entities:
            if e.kind == "npc" and known_hostile(w, e):
                packs.setdefault((w.map_id, e.pos[0] // PACK_CELL, e.pos[1] // PACK_CELL), []).append(e)
        signs = {(mid, pos) for mid, view in w.maps.items() for pos in view.readable}
        finds: list[dict[str, Any]] = []
        if self.primed:
            for i in sorted(npcs.keys() - self.npcs):
                e = npcs[i]
                finds.append({"kind": "npc", "id": e.id, "type": e.code or None, "cell": list(e.pos)})
            for mid, pos in sorted(entrances - self.entrances):
                finds.append({"kind": "entrance", "map_id": mid, "cell": list(pos)})
            for code in sorted(affordable.keys() - self.affordable):
                finds.append({"kind": "affordable", "code": code, "price": affordable[code], "gems": gems})
            for key in sorted(packs.keys() - self.packs, key=str):
                members = packs[key]
                types = sorted({e.code for e in members if e.code})
                finds.append({"kind": "hostile_pack", "count": len(members), "types": types, "cell": list(members[0].pos)})
            for mid, pos in sorted(signs - self.signs):
                if not cell_was_read(knowledge, mid, pos):
                    finds.append({"kind": "sign", "map_id": mid, "cell": list(pos)})
        self.npcs |= npcs.keys()
        self.entrances |= entrances
        self.affordable = set(affordable)  # an item the gems fell short of can be new again
        self.packs |= packs.keys()
        self.signs |= signs
        self.primed = True
        return finds
