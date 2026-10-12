"""Crossing water with a fording supply (A133).

A raft, or any supply whose Supplies reference row has ``fords``, makes water
walkable while it is armed or worn (Manual, Supplies capability table). While
the character stands on water, an ``Arm``, ``Wear``, ``Remove`` or ``Drop``
that would leave it with no fording supply armed or worn is refused
``would_strand``.

So there is one rule, applied to every queue a state sends
(``states.dispatch``):

- a walk plans over water while a fording supply is carried, held or armed
  (``WorldModel.fords``, ``MapView.walkable``);
- before the first ``Step`` onto water, the queue arms a held fording supply
  unless one is armed or worn already (``ford_queue``);
- on water, the queue stops before any loadout change that would strand: no
  state re-arms a weapon, cutter or potion mid-water;
- under attack on water with only the fording supply in hand, the
  character makes for the nearest land (``states.ashore``);
- ashore, with a fording supply armed and no water step in the queue, the
  queue first arms the weapon the one weapon rule picks (A99,
  ``equip.weapon_to_arm``), unless it arms something itself.

A ``would_strand`` refusal marks the cell the character stands on as water
(``note_would_strand``, ``sync``), so a cell the map did not show as water is
not tried again.
"""

from __future__ import annotations

from .equip import FIGHT, hunting_ceiling, weapon_to_arm
from .executor.movement import step_landing
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .supplies import fords, worn_slot
from .world import WATER, Pos, WorldModel

LOADOUT_VERBS = ("Arm", "Wear", "Remove", "Drop")
WOULD_STRAND = "would_strand"


def note_would_strand(m: Memory, code: str | None) -> None:
    """A ``would_strand`` refusal: the character stands on water. Where is
    placed at the next decision, once the observation has moved it (``sync``)."""
    if code == WOULD_STRAND:
        m.stranded = True


def sync(w: WorldModel, m: Memory) -> None:
    """Place a ``would_strand`` refusal on the cell the character stands on."""
    if m.stranded and w.map_id is not None and w.pos is not None:
        m.water_at = (w.map_id, w.pos)
    m.stranded = False


def on_water(w: WorldModel, m: Memory, p: Pos) -> bool:
    """Whether ``p`` on the current map is water: the map says so, or a
    ``would_strand`` refusal came from it."""
    return w.view.tiles.get(p) == WATER or m.water_at == (w.map_id, p)


def afloat(armed: str | None, worn: list[str]) -> bool:
    """A fording supply is armed or worn: water may be stepped on."""
    return fords(armed) or any(fords(code) for code in worn)


def stranded_unarmed(w: WorldModel) -> bool:
    """Only a fording supply is in hand and none is worn: on water no weapon
    can be armed, so there is no fight to have (``states.ashore``)."""
    return fords(w.armed_code) and not any(fords(code) for code in w.worn_codes.values())


def ford_queue(
    w: WorldModel, m: Memory, intents: list[dict], kb: KnowledgeBase | None = None
) -> tuple[list[dict], str]:
    """``intents`` made safe to send over water, and why it was cut short ("" when it was not).

    The queue is followed from where the character stands: before the first
    move onto water with no fording supply armed or worn, a held one is
    armed; a loadout change on water that would leave none armed or worn
    ends the queue there. A queue that moves onto water with nothing to ford
    with ends before that move. Ashore with a fording supply armed, a queue
    that neither moves onto water nor arms anything first arms the weapon to
    fight with (``rearm_ashore``).
    """
    if not intents or w.map_id is None or w.pos is None or not w.fords:
        return intents, ""
    pos = w.pos
    armed, armed_id = w.armed_code, w.armed_id
    worn = dict(w.worn_codes)
    held = {h.id: h.code for h in w.held_supplies}
    out: list[dict] = []
    crosses = False  # the queue moves onto water
    for intent in intents:
        verb = intent.get("verb")
        if verb in ("Step", "SetPosition"):
            landing = step_landing(pos, intent["direction"]) if verb == "Step" else (intent["x"], intent["y"])
            crosses = crosses or on_water(w, m, landing)
            if on_water(w, m, landing) and not afloat(armed, list(worn.values())):
                raft = next((sid for sid, code in held.items() if fords(code)), None)
                if raft is None:
                    return out, f"no fording supply to arm before the water at {landing[0]},{landing[1]}"
                out.append({"verb": "Arm", "supply_id": raft})
                armed, armed_id = _arm(held, armed, armed_id, raft)
            pos = landing
        elif verb in LOADOUT_VERBS:
            after = _loadout_after(intent, held, armed, armed_id, worn)
            if on_water(w, m, pos) and afloat(armed, list(worn.values())) and not afloat(after[0], list(after[2].values())):
                return out, f"{verb} would strand us on the water at {pos[0]},{pos[1]}"
            armed, armed_id, worn = after
        out.append(intent)
    if not crosses and not any(i.get("verb") == "Arm" for i in out):
        out = rearm_ashore(w, m, kb) + out
    return out, ""


def rearm_ashore(w: WorldModel, m: Memory, kb: KnowledgeBase | None) -> list[dict]:
    """``[Arm weapon]`` when the character stands ashore with a fording
    supply armed that is no weapon: the one weapon rule (A99) picks the
    weapon. Nothing while Heal, Solve or Break holds the armed slot."""
    if w.pos is None or on_water(w, m, w.pos) or not fords(w.armed_code):
        return []
    if m.solve_rearm is not None or m.break_rearm is not None or m.self_use_rearm is not None:
        return []
    weapon = weapon_to_arm(w, kb, FIGHT, hunting_ceiling(w, kb), m.equip_refused)
    if weapon is None or weapon.code == w.armed_code or weapon.id < 0:
        return []
    return [{"verb": "Arm", "supply_id": weapon.id}]


def _arm(held: dict[int, str], armed: str | None, armed_id: int | None, sid: int) -> tuple[str | None, int | None]:
    """Arm ``sid`` from ``held``: what was armed goes back to the hand."""
    code = held.pop(sid, None)
    if code is None:
        return armed, armed_id
    if armed is not None and armed_id is not None:
        held[armed_id] = armed
    return code, sid


def _loadout_after(
    intent: dict, held: dict[int, str], armed: str | None, armed_id: int | None, worn: dict[str, str]
) -> tuple[str | None, int | None, dict[str, str]]:
    """Armed code and id and worn slots once ``intent`` (a loadout verb) runs."""
    verb, sid = intent.get("verb"), intent.get("supply_id")
    worn = dict(worn)
    if verb == "Arm" and sid is not None:
        armed, armed_id = _arm(held, armed, armed_id, sid)
    elif verb == "Wear" and sid in held:
        code = held.pop(sid)
        worn[worn_slot(code) or f"slot of {sid}"] = code
    elif verb == "Remove":
        worn.pop(intent.get("slot", ""), None)
    elif verb == "Drop":
        if sid is not None and sid == armed_id:
            armed, armed_id = None, None
        held.pop(sid, None)
    return armed, armed_id, worn
