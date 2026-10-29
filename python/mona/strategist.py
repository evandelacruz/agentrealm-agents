"""Mona's strategist: the kit, and picks from a fixed list (docs/v4/SPEC.md **The strategist**).

Asked only at decision points, off the decision thread; its answer stands
until the next. Its standing context, sent once and cached, is the Manual's
world, rules and play guide (§3, §11, §16) and ``docs/GAME_NOTES.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agentrealm_adk import supplies
from agentrealm_adk.llm import Backoff, CallBudget, LLMClient, ProviderConfig, connect, parse_reply, trace_messages
from agentrealm_adk.planner_reference import REFERENCE_PATH, game_notes_text, read_text, split_sections
from agentrealm_adk.world import chebyshev

from adk3.realm.context import Ctx

from .kit import Kit

MANUAL = ("/docs/manual / 3. how the world works", "/docs/manual / 11. game rules", "/docs/manual / 16. playing the world")
SHOPS_SHOWN = 30

CONTRACT = """\
You are the strategist of Modal Mona, an agent playing Agent Realm. The objective: clear every level.

Her play is fixed rules. She is always in one of four modes, chosen by a fixed rule from the kit you write:
1. learn what level N needs (the kit is unknown or not trusted),
2. get money (her gems do not cover the kit),
3. get gear (something on the kit is missing),
4. beat level N.
N is the lowest level not cleared. Under the modes, fixed reflexes play every tick, before anything else: step off fire or lava; at 3 health or below with a hostile near, drink a potion; a hostile within 10 blocks is fought when she would win and escaped when she would lose (fought if cornered); take gems, gear, chests and, when hurt, food; read signs and talk to people. At 3 health or below she goes for food until she is back to 10.

You do two things only.

A. The kit for level N, when asked: the gear she must carry, arm or wear to set off for it, and whether you trust it. The kit is a gate: checked once when she sets off, never on the way or inside, so what she spends out there does not send her back to buy it again; a death or a changed kit sends her back through the gate. (Other destinations have fixed kits: the outer band wants a bronze sword, bronze mail and a potion; the inner band, town and shops want nothing.) Reply:
{"level": N, "entrance": "<entrance key from the list, or null>", "items": ["<item code>", ...], "potions": 2, "trusted": true, "why": "..."}
- items: the weapon and armor to fight its boss (potions go in "potions", not here), and whatever its entrance needs (a key, a bomb for a hidden door, a raft for water). Item codes from the Supplies reference; only items sold or found in this world.
- entrance: the entrance you judge is level N's, from the clues (rumor helpers point at level doors).
- trusted: true when you would send her in with this kit. While learning, untrusted means "keep learning": she walks town for signs and helpers, then scouts the entrance, then attempts the level. After an attempt you must trust a kit.
- A kit that failed (a death, a loss, an entrance that refused her, an item sold nowhere she could find) must change: more armor, a better weapon, the tool the door needs, an item that is sold.

B. A pick from a list, when asked: {"pick": <index>, "why": "..."}.

Reply with one JSON object and nothing else.
"""


@dataclass
class Ask:
    """What the strategist is asked: ``kind`` is "kit" or "pick"."""

    kind: str
    ctx: Ctx
    kit: Kit | None
    level: int
    reason: str
    options: list[str] | None = None
    flow: object = None  # the mode flow that asked for a pick: its answer goes back to it alone


class Strategist:
    def __init__(self, client: LLMClient, cfg: ProviderConfig, record=None):
        self.client, self.cfg, self.record = client, cfg, record
        self.budget, self.backoff = CallBudget(), Backoff()
        sections = [s.text for s in split_sections(read_text(REFERENCE_PATH)) if s.key.startswith(MANUAL)]
        self.system = (CONTRACT + "\nThe Manual (world, game rules, playing the world):\n" + "".join(sections)
                       + "\nGame notes (facts learned in play):\n" + game_notes_text())

    @classmethod
    def from_env(cls, record=None) -> Strategist:
        cfg = ProviderConfig.from_env()
        return cls(connect(cfg), cfg, record)

    def __call__(self, ask: Ask) -> dict[str, Any]:
        now = time.time()
        if self.backoff.waiting(now):
            raise RuntimeError("backing off after a failed call")
        messages = [{"role": "system", "content": self.system, "cache": True},
                    {"role": "user", "content": describe(ask)}]
        self.budget.charge(now, messages)
        try:
            raw, usage = self.client.complete(messages)
        except Exception:
            self.backoff.fail(time.time())
            raise
        self.budget.settle(usage)
        self.backoff.ok()
        if self.record is not None:
            self.record({"t": now, "strategist": {"kind": ask.kind, "reason": ask.reason,
                                                  "messages": trace_messages(messages[1:]), "reply": raw}})
        answer = parse_reply(raw, lambda o: isinstance(o, dict))
        if not isinstance(answer, dict):
            raise ValueError("the reply is not a JSON object")
        return answer


def kit_of(answer: dict[str, Any], ask: Ask) -> Kit:
    """The answer as a kit for the level asked about; after an attempt it is trusted whatever it says."""
    learned = list(ask.kit.learned) if ask.kit is not None and ask.kit.level == ask.level else []
    listed = [str(c) for c in answer.get("items") or [] if c]
    items = [c for c in listed if not supplies.is_potion(c)]  # potions are counted in "potions", not listed twice
    potions = max(int(answer.get("potions") or 0), len(listed) - len(items))
    trusted = bool(answer.get("trusted")) or "attempt" in learned
    return Kit(level=ask.level, entrance=answer.get("entrance") or None, items=items, potions=potions,
               trusted=trusted, why=str(answer.get("why", "")), learned=learned)


def pick_of(answer: dict[str, Any], ask: Ask) -> int:
    try:
        i = int(answer.get("pick"))
    except (TypeError, ValueError):
        return 0
    return i if 0 <= i < len(ask.options or []) else 0


def describe(ask: Ask) -> str:
    ctx = ask.ctx
    s, mem = ctx.state, ctx.memory
    lines = [f"Asked because: {ask.reason}", "", "## Mona now",
             f"health {s.health}/{s.max_health}, lives {s.lives}, gems {s.gems}",
             "carried: " + (", ".join(f"{c.code} ({c.where})" for c in s.carried) or "nothing"),
             f"levels cleared: {list(s.levels_cleared)} of {s.level_count or '?'}; level N is {ask.level}",
             f"on map {s.map_id} at {s.pos[0]},{s.pos[1]}"]
    if ctx.town:
        lines.append(f"town (shops, berries, respawn): map {ctx.town[0]} near {ctx.town[1][0]},{ctx.town[1][1]}")
    k = ask.kit
    lines += ["", "## The kit now"]
    lines.append(f"level {k.level}, entrance {k.entrance}, items {k.items}, potions {k.potions}, trusted {k.trusted}, "
                 f"learn steps done {k.learned}, set off {'yes' if k.set_off else 'no'}; why: {k.why}" if k else "none yet")
    lines += ["", "## Entrances (keys as listed; unnumbered until entered)"]
    for key, e in sorted(mem.entrances.items(), key=lambda kv: chebyshev((kv[1].x, kv[1].y), s.pos)):
        facts = [f"block {e.block}" if e.block else "not seen up close", "reached" if e.reached else "",
                 f"refused us: {e.refused}" if e.refused else "", f"opens level {e.level}" if e.level else "",
                 f"{chebyshev((e.x, e.y), s.pos)} blocks away" if e.map_id == s.map_id else ""]
        lines.append(f"- {key}: " + ", ".join(f for f in facts if f))
    if mem.levels:
        lines += ["", "## Levels"]
        for lv in sorted(mem.levels.values(), key=lambda lv: lv.number):
            tries = "; ".join(f"{a.result} after {a.seconds:.0f}s, boss health {a.boss_health_left}" for a in lv.attempts[-3:])
            lines.append(f"- level {lv.number}: entrance {lv.entrance or '?'}, boss {lv.boss or '?'}"
                         f" (max health {lv.boss_max_health or '?'}){'; attempts: ' + tries if tries else ''}")
    lines += ["", "## Every clue so far"]
    lines += [f"- {c.source} {c.speaker} at {c.map_id}:{c.x},{c.y}: {c.text}" for c in mem.clues] or ["none yet"]
    lines += ["", "## Seen for sale"]
    shops = sorted(mem.shops.values(), key=lambda v: (v.map_id != s.map_id, chebyshev((v.x, v.y), s.pos)))
    lines += [f"- {v.code} for {v.price} gems at {v.map_id}:{v.x},{v.y}" for v in shops[:SHOPS_SHOWN]] or ["nothing yet"]
    if mem.deaths:
        lines += ["", "## Deaths (newest last)"]
        lines += [f"- killed by {d.killer or '?'} at {d.map_id}:{d.x},{d.y}" for d in mem.deaths[-5:]]
    if ask.kind == "pick":
        lines += ["", f"## Pick {ask.reason}"] + [f"{i}: {o}" for i, o in enumerate(ask.options or [])]
        lines.append('Reply {"pick": <index>, "why": "..."}.')
    else:
        lines.append("\nReply with the kit for level N.")
    return "\n".join(lines)
