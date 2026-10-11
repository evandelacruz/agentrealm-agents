"""M8 acceptance metrics (A25): gear, economy and combat on the overworld.

The M8 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks for each clause:

- Earns gems: the gem counter rises at least once during the run.
- Buys armor, a weapon and a potion reserve: body-or-better armor is worn,
  a shop weapon is armed (not the starting pocket knife), and held plus stowed
  potions reach ``potion_reserve`` at least once.
- Heals from food it picks up and from carried potions: while hurt, our
  character takes a supply the Supplies reference marks food (a
  ``SupplyTaken`` naming us as ``taker_id``, which only the taker receives),
  however it was taken (a ``Take``, or a step onto food eaten on pickup) and
  by whichever state (**Heal**, or **Detour** on the way), and a self-``Use`` **Heal** sends with a potion in
  hand uses one up (A76): its result is ``applied`` and the held plus stowed
  potion count then drops below what it was when the drink was sent, or a
  ``SupplyUsed`` event names our character and a potion. A ``Use`` that
  applies and leaves the potions as they were drank nothing and does not
  count.
- Kills lone weak hostiles without dying: a kill is an ``NPCDied`` with an
  ``NPCDamaged`` on that NPC from our character on the same tick, the killing
  blow (API Events), whichever state swung it and however many polls of a held
  queue later it lands. It is a lone weak kill when, at our first attack on
  that NPC, it was the whole combat group and its type's damage per hit was
  at most the world's base attack power (2): the price the win estimate gives
  it, measured or not. Deaths fail the run immediately.
- Never starts a fight below its health floor: the first attack ``Use`` after
  entering **Fight** must not come while ``would_lose`` holds (the group's
  expected damage against health plus ``fight_margin``; PLAYABLE_AGENT
  Combat). Swings later in the same fight are not judged: taking hits is what
  a fight does.

On a run shorter than 95% of the target duration, only deaths, fight-floor
violations and API errors fail the run; the milestone checks above are judged
only on a nearly full run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .acceptance_run import FULL_RUN_FRACTION, TimedRunHooks  # FULL_RUN_FRACTION: re-exported for the smoke script
from .config import Policy
from .equip import is_consumable, is_weapon, wear_slot
from .healing import code_in_hand, hurt, potion_count
from .supplies import is_food, is_potion
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .survival import combat_group, would_lose
from .threat import UNMEASURED_DEFAULT, type_key_for_entity
from .states.intents import is_self_use
from .world import WorldModel

TARGET_SECONDS = 3600.0
STARTING_WEAPON = "pocket_knife"


@dataclass(kw_only=True)
class M8AcceptanceMetrics(TimedRunHooks):
    """Counts economy, healing, combat and API faults while the runner plays."""

    target_seconds: float = TARGET_SECONDS
    potion_reserve: int = 2
    start_gems: int | None = None
    max_gems: int | None = None
    gems_earned: bool = False
    armor_worn: bool = False
    shop_weapon: bool = False
    potion_reserve_met: bool = False
    food_taken_hurt: bool = False
    heal_potion: bool = False
    kills: int = 0
    weak_hostile_kills: int = 0
    fight_below_floor: int = 0
    _prev_gems: int | None = field(default=None, repr=False)
    _seen_worn: set[str] = field(default_factory=set, repr=False)
    _fighting: bool = field(default=False, repr=False)  # an attack was sent since entering Fight
    # Per NPC in sight we attacked: whether it was a lone weak hostile at our
    # first attack on it.
    _lone_weak: dict[int, bool] = field(default_factory=dict, repr=False)
    # Food supplies in sight at the last decision, id to code, for our
    # ``SupplyTaken`` (which arrives after the supply has left the entity list).
    _food_seen: dict[int, str] = field(default_factory=dict, repr=False)
    _hurt: bool = field(default=False, repr=False)  # at the last decision
    _character_id: int | None = field(default=None, repr=False)
    # Heal's potion drink still out: the potion count when it was sent (A76).
    # It is judged on the first decision after its applied result, whose
    # observation carries the inventory, and dropped when it is refused or
    # used nothing up, when a queue without it replaces it, or on a death.
    _drink_potions: int | None = field(default=None, repr=False)
    _drink_applied: bool = field(default=False, repr=False)
    # A queue without the drink was sent: forget it once that response's
    # results and events are read, which may still carry the drink's own
    # (as ``Runner._forget_replaced_drink``).
    _drink_replaced: bool = field(default=False, repr=False)

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
        acted_op: dict | None = None,
        plan_op: dict | None = None,
    ) -> None:
        if state != "Fight":
            self._end_fight()
        self.potion_reserve = max(0, int(params.get("potion_reserve", self.potion_reserve)))
        self._note_gems(w)
        self._note_loadout(w)
        self._character_id = w.character_id
        self._note_drink_used_up(w)
        self._hurt = hurt(w)
        self._food_seen = {e.id: e.code for e in w.entities if e.kind == "supply" and is_food(e.code)}
        in_sight = {e.id for e in w.entities if e.kind == "npc"}
        self._lone_weak = {npc: v for npc, v in self._lone_weak.items() if npc in in_sight}
        if potion_count(w) >= self.potion_reserve:
            self.potion_reserve_met = True
        if intents is not None:
            self._note_drink(state, intents, w)
            self._note_attacks(intents, w, policy)
            self._note_fight(state, intents, w, policy, params)

    def on_intent_result(self, intent: dict | None, result: dict) -> None:
        if self._drink_potions is None or not is_self_use(intent):
            return
        if result.get("outcome") == "applied":
            self._drink_applied = True  # the count drop is read on the next decision
        else:
            self._drink_potions = None  # refused, or used nothing up

    def on_events(self, events: list[dict]) -> None:
        for ev in events:
            kind = ev.get("kind")
            if kind == "SupplyUsed":
                self._note_supply_used(ev)
            elif kind == "SupplyTaken":
                self._note_supply_taken(ev)
            elif kind == "NPCDied":
                self._note_npc_died(ev, events)
        if self._drink_replaced and not self._drink_applied:
            self._drink_potions = None
        self._drink_replaced = False

    def on_death(self) -> None:
        super().on_death()
        self._drink_potions, self._drink_applied = None, False
        self._lone_weak.clear()

    def _end_fight(self) -> None:
        self._fighting = False

    def _ours(self, ev: dict) -> bool:
        """The event names our character as its actor."""
        if self._character_id is None:
            return False
        return ev.get("actor_kind") == "character" and ev.get("actor_id") == self._character_id

    def _note_npc_died(self, died: dict, events: list[dict]) -> None:
        """Our kill: our ``NPCDamaged`` on the dead NPC lands on the tick it died."""
        npc, tick = died.get("npc_id"), died.get("tick")
        lone_weak = self._lone_weak.pop(npc, False)
        killing_blow = tick is not None and any(
            ev.get("kind") == "NPCDamaged"
            and ev.get("npc_id") == npc
            and ev.get("tick") == tick
            and self._ours(ev)
            for ev in events
        )
        if not killing_blow:
            return
        self.kills += 1
        if lone_weak:
            self.weak_hostile_kills += 1

    def _note_supply_taken(self, ev: dict) -> None:
        """We took food while hurt. Only the taker receives ``SupplyTaken``,
        but the check on ``taker_id`` keeps it ours."""
        code = self._food_seen.pop(ev.get("supply_id"), None)
        if code is None or self._character_id is None or ev.get("taker_id") != self._character_id:
            return
        if self._hurt:
            self.food_taken_hurt = True

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

    def _note_supply_used(self, ev: dict) -> None:
        """Our own ``SupplyUsed`` for a potion while Heal's drink is out: it was drunk.

        The event reaches every character in sight of the block, so another
        character's drink is not ours."""
        if self._drink_potions is None or not is_potion(ev.get("supply_code")):
            return
        if self._character_id is not None and ev.get("actor_id") == self._character_id:
            self.heal_potion = True
            self._drink_potions = None

    def _note_drink_used_up(self, w: WorldModel) -> None:
        """An applied drink counts only if the potion count shows one gone.

        A ``Use`` that applied and left the potions as they were drank nothing
        (A67 run 4: potions 2 -> 2)."""
        if self._drink_potions is None or not self._drink_applied:
            return
        if potion_count(w) < self._drink_potions:
            self.heal_potion = True
        self._drink_potions, self._drink_applied = None, False

    def _note_drink(self, state: str, intents: list[dict], w: WorldModel) -> None:
        """Heal's potion drink, whose result decides ``heal_potion``.

        A sent queue replaces the one before it, so a drink still out is
        forgotten (after this response, ``on_events``) unless this queue
        carries a new one."""
        drink = state == "Heal" and any(
            is_self_use(intent) and is_potion(code_in_hand(w, intents, i))
            for i, intent in enumerate(intents)
        )
        if drink:
            self._drink_potions, self._drink_applied = potion_count(w), False
        self._drink_replaced = not drink

    def _note_attacks(self, intents: list[dict], w: WorldModel, policy: Policy) -> None:
        """At our first attack on an NPC, from any state, whether it is a lone weak hostile."""
        for intent in intents:
            if not _is_attack_use(intent, w):
                continue
            npc = (intent.get("target") or {}).get("npc_id")
            if npc is None or npc in self._lone_weak:
                continue
            target = next((e for e in w.entities if e.kind == "npc" and e.id == npc), None)
            self._lone_weak[npc] = target is not None and _lone_weak_group(w, combat_group(w, policy, target), npc)

    def _note_fight(
        self,
        state: str,
        intents: list[dict],
        w: WorldModel,
        policy: Policy,
        params: dict[str, float | int],
    ) -> None:
        if state != "Fight" or not intents:
            return
        attacks = [i for i in intents if _is_attack_use(i, w)]
        if not attacks:
            return
        if not self._fighting and would_lose(w, policy, params):
            self.fight_below_floor += 1
        self._fighting = True

    def milestones_ok(self) -> bool:
        return (
            self.gems_earned
            and self.armor_worn
            and self.shop_weapon
            and self.potion_reserve_met
            and self.food_taken_hurt
            and self.heal_potion
            and self.weak_hostile_kills >= 1
        )

    def failures(self, *, full_run: bool = True) -> list[str]:
        out = list(self.base_failures())
        if self.fight_below_floor:
            out.append(
                f"{self.fight_below_floor} fight(s) started below the health floor (would_lose)"
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
        if not self.food_taken_hurt:
            out.append("never took ground food while hurt")
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
            f"food taken while hurt: {self.food_taken_hurt}, heal potion: {self.heal_potion}",
            f"kills: {self.kills}, lone weak: {self.weak_hostile_kills}",
            f"fight below health floor: {self.fight_below_floor}",
            f"deaths: {self.deaths}",
            f"API errors: {len(self.api_errors)}",
            self.planner_summary_line(),
        ]


def _is_armor(code: str, w: WorldModel) -> bool:
    if is_weapon(code) or is_consumable(code):
        return False
    slot = wear_slot(code, w)
    return slot is not None and slot != "accessory"


def _is_attack_use(intent: dict, w: WorldModel) -> bool:
    """A ``Use`` at a character, an NPC or a block; a self ``Use`` is a drink."""
    if intent.get("verb") != "Use":
        return False
    return (intent.get("target") or {}).get("kind") in ("character", "npc", "block")


def _lone_weak_group(w: WorldModel, group: list, npc_id: int) -> bool:
    """``npc_id`` is the whole group, and hits no harder than the world's base
    attack power at the price the win estimate gives it (a type never
    measured is priced at that base)."""
    if len(group) != 1 or group[0].kind != "npc" or group[0].id != npc_id:
        return False
    return w.threat.damage_per_hit(type_key_for_entity(group[0])) <= UNMEASURED_DEFAULT
