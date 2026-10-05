"""M8 acceptance metrics (A25): gear, economy and combat on the overworld.

The M8 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks for each clause:

- Earns gems: the gem counter rises at least once during the run.
- Buys armor, a weapon and a potion reserve: body-or-better armor is worn,
  a shop weapon is armed (not the starting pocket knife), and held plus stowed
  potions reach ``potion_reserve`` at least once.
- Heals from food it picks up and from carried potions: **Heal** sends a
  ``Take`` on ground food, and **Heal** sends ``Arm`` + ``Use`` self on a
  carried potion.
- Kills lone weak hostiles without dying: at least one ``NPCDied`` while the
  agent was fighting a lone measured hostile whose threat table hit is at most
  the weak default (2). Deaths fail the run immediately.
- Never starts a fight below its health floor: **Fight** must not send an
  attack ``Use`` while hurt (health below ``max_health``; PLAYABLE_AGENT
  Combat, "never start a fight hurt").

On a run shorter than 95% of the target duration, only deaths, fight-floor
violations and API errors fail the run; the milestone checks above are judged
only on a nearly full run.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance_run import TimedRunHooks
from .break_memory import WEAPONS
from .config import Policy
from .equip import is_consumable, is_weapon, wear_slot
from .healing import FOOD_CODES, POTION_CODES, hurt, potion_count
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .survival import combat_group
from .threat import UNMEASURED_DEFAULT, type_key_for_entity
from .world import WorldModel

TARGET_SECONDS = 3600.0
STARTING_WEAPON = "pocket_knife"


@dataclass(kw_only=True)
class M8AcceptanceMetrics(TimedRunHooks):
    """Counts economy, healing, combat and API faults while the runner plays."""

    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    potion_reserve: int = 2
    start_gems: int | None = None
    max_gems: int | None = None
    gems_earned: bool = False
    armor_worn: bool = False
    shop_weapon: bool = False
    potion_reserve_met: bool = False
    heal_food_take: bool = False
    heal_potion: bool = False
    weak_hostile_kills: int = 0
    fight_below_floor: int = 0
    _prev_gems: int | None = field(default=None, repr=False)
    _seen_worn: set[str] = field(default_factory=set, repr=False)
    _weak_lone_fight: bool = field(default=False, repr=False)

    def before_tick(
        self,
        w: WorldModel,
        m: Memory,
        *,
        state: str,
        reason: str,
        intents: list[dict] | None,
        policy: Policy,
        params: dict[str, float | int],
        knowledge: KnowledgeBase | None,
    ) -> None:
        self._weak_lone_fight = False
        self.potion_reserve = max(0, int(params.get("potion_reserve", self.potion_reserve)))
        self._note_gems(w)
        self._note_loadout(w)
        if potion_count(w) >= self.potion_reserve:
            self.potion_reserve_met = True
        if intents is not None:
            self._note_heal(state, intents, w)
            self._note_fight(state, intents, w, policy)

    def on_events(self, events: list[dict]) -> None:
        for ev in events:
            if ev.get("kind") == "NPCDied" and self._weak_lone_fight:
                self.weak_hostile_kills += 1

    def _note_gems(self, w: WorldModel) -> None:
        if w.gems is None:
            return
        if self.start_gems is None:
            self.start_gems = w.gems
        if self._prev_gems is not None and w.gems > self._prev_gems:
            self.gems_earned = True
        self._prev_gems = w.gems
        self.max_gems = w.gems if self.max_gems is None else max(self.max_gems, w.gems)

    def _note_loadout(self, w: WorldModel) -> None:
        for code in w.worn_codes.values():
            if not code or code in self._seen_worn:
                continue
            self._seen_worn.add(code)
            if _is_armor(code, w):
                self.armor_worn = True
        weapon = w.armed_code
        if weapon and is_weapon(weapon) and weapon != STARTING_WEAPON:
            self.shop_weapon = True

    def _note_heal(self, state: str, intents: list[dict], w: WorldModel) -> None:
        if state != "Heal":
            return
        for intent in intents:
            if intent.get("verb") == "Take":
                sid = intent.get("supply_id")
                for e in w.entities:
                    if e.kind == "supply" and e.id == sid and e.code in FOOD_CODES:
                        self.heal_food_take = True
            if intent.get("verb") == "Use" and _is_self_use(intent, w):
                code = w.armed_code
                if code in POTION_CODES:
                    self.heal_potion = True

    def _note_fight(self, state: str, intents: list[dict], w: WorldModel, policy: Policy) -> None:
        if state != "Fight" or not intents:
            return
        attacks = [i for i in intents if _is_attack_use(i, w)]
        if not attacks:
            return
        if hurt(w):
            self.fight_below_floor += 1
        group = combat_group(w, policy)
        self._weak_lone_fight = _lone_weak_group(w, group)

    def milestones_ok(self) -> bool:
        return (
            self.gems_earned
            and self.armor_worn
            and self.shop_weapon
            and self.potion_reserve_met
            and self.heal_food_take
            and self.heal_potion
            and self.weak_hostile_kills >= 1
        )

    def failures(self, *, full_run: bool = True) -> list[str]:
        out = list(self.base_failures())
        if self.fight_below_floor:
            out.append(
                f"{self.fight_below_floor} Fight attack(s) while hurt (below health floor)"
            )
        if not full_run:
            return out
        if not self.gems_earned:
            out.append("gems never increased during run")
        if not self.armor_worn:
            out.append("no armor worn")
        if not self.shop_weapon:
            out.append("no shop weapon armed (still pocket knife or unarmed)")
        if not self.potion_reserve_met:
            out.append(f"potion reserve {self.potion_reserve} never reached")
        if not self.heal_food_take:
            out.append("Heal never took ground food")
        if not self.heal_potion:
            out.append("Heal never drank a carried potion")
        if self.weak_hostile_kills < 1:
            out.append("no lone weak hostile kill recorded")
        return out

    def summary_lines(self) -> list[str]:
        return [
            f"gems: start {self.start_gems}, max {self.max_gems}, earned: {self.gems_earned}",
            f"armor worn: {self.armor_worn}, shop weapon: {self.shop_weapon}",
            f"potion reserve {self.potion_reserve} met: {self.potion_reserve_met}",
            f"heal food take: {self.heal_food_take}, heal potion: {self.heal_potion}",
            f"weak hostile kills: {self.weak_hostile_kills}",
            f"fight below health floor: {self.fight_below_floor}",
            f"deaths: {self.deaths}",
            f"API errors: {len(self.api_errors)}",
        ]


def _is_armor(code: str, w: WorldModel) -> bool:
    if is_weapon(code) or is_consumable(code):
        return False
    slot = wear_slot(code, w)
    return slot is not None and slot != "accessory"


def _is_self_use(intent: dict, w: WorldModel) -> bool:
    target = intent.get("target") or {}
    return target.get("kind") == "character" and target.get("character_id") == w.character_id


def _is_attack_use(intent: dict, w: WorldModel) -> bool:
    if intent.get("verb") != "Use":
        return False
    target = intent.get("target") or {}
    kind = target.get("kind")
    if kind == "character":
        return target.get("character_id") != w.character_id
    return kind in ("npc", "block")


def _lone_weak_group(w: WorldModel, group: list) -> bool:
    if len(group) != 1:
        return False
    key = type_key_for_entity(group[0])
    if key is None or not w.threat.measured(key):
        return False
    return w.threat.damage_per_hit(key) <= UNMEASURED_DEFAULT
