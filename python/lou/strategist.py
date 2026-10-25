"""Lou's strategist: owns strategy (docs/v3/SPEC.md **The strategist**).

Its standing context, sent once and cached, is the Manual's world, game
rules and play guide (§3, §11, §16) and ``docs/GAME_NOTES.md``. Each ask
carries Lou's state, her memory, what is running, every outcome since it
last spoke, and the chain of needs it stated last time. It answers with the
chain, the first unmet dependency as the goal, an approach to it, a plan B
and the goal after it (``lou.publish`` reads that into a publication).
"""

from __future__ import annotations

import time
from typing import Any

from agentrealm_adk.llm import Backoff, CallBudget, LLMClient, ProviderConfig, connect, parse_reply, trace_messages
from agentrealm_adk.planner_reference import REFERENCE_PATH, game_notes_text, read_text, split_sections
from agentrealm_adk.world import chebyshev

from adk3.core.loop import Briefing
from adk3.realm.context import Ctx

MANUAL = ("/docs/manual / 3. how the world works", "/docs/manual / 11. game rules", "/docs/manual / 16. playing the world")
SHOPS_SHOWN = 30

CONTRACT = """\
You are the strategist of Layered Lou, an agent playing Agent Realm. The objective: beat the world by clearing every level.

You own strategy. Work backwards from beating the game, the way a person would: beat the game <- clear the levels <- clear this level <- carry the gear it needs <- hold the gems the gear costs <- cut grass for them. State that chain, then publish its first unmet dependency as the goal, with an approach to it, a plan B, and the goal after it.

How Lou carries it out. The executive runs your approach's method and checks your goal's test and your failure conditions every decision. When the approach fails it switches to plan B at once; when the goal is met it starts "next" at once; with nothing left it does safe work near town until you answer. Real-time reactions run without you, in fixed priority: combat (an engagement planned once as reward against risk in gems: fight to the end unless health reaches the bar, where she drinks or breaks off to safe ground; back off until clear; or go on), the boss fight on its clock, opportunities in view worth their detour (gems, food when hurt, signs, helpers, odd blocks, her death chest), then your method. Each reaction's outcome is reported back to you under "What just happened". Her temperament is fixed by her levers (below); work within them.

Read the clues: the world tells you how to progress through text (rumor helpers point at level doors, waystation helpers say what locked doors need). Use the Manual below for prices, where each gear tier is sold, where gems come from, how hostiles and bosses behave.

Reply with one JSON object and nothing else:
{
  "chain": ["beat the world", "clear level ?: the entrance at 76:12,40", "carry bronze_sword", "hold 15 gems"],
  "why": "your reasoning, briefly",
  "goal": {"test": "gems", "n": 15, "text": "hold 15 gems"},
  "approach": {"method": "earn_gems", "x": 400, "y": 440, "text": "cut grass in the field around 400,440",
               "fails_if": {"no_gem_minutes": 3, "minutes": 15},
               "tactics": {"bars": {"snotling": 3}, "avoid": ["gristlewick"], "detour": 10}},
  "plan_b": {"method": "earn_gems", "x": 360, "y": 450, "fails_if": {"no_gem_minutes": 3}},
  "next": {"goal": {"test": "carry", "item": "bronze_sword"}, "why": "...",
           "approach": {"method": "buy", "item": "bronze_sword"}},
  "notes": "what to remember for next time"
}

Goal tests (the executive checks them; pick the one that says the dependency is met):
- gems {n}: holding at least n gems. carry {item, count?}: carrying the item. equipped {item}: armed or worn.
- cleared {level}. inside {entrance}: inside the level behind that entrance key. at {x, y, map?}: standing there.
- health {n}. seller {item}: a shelf selling it has been seen. found {what}: in view, what is door, sign, entrance, npc:<type> or supply:<code>.
- boss_seen: the level's boss is in view.

Methods (what an approach does):
- earn_gems {x?, y?}: cut grass in the field around x,y (near Lou when left out), taking the drops. Hostiles there are fought or not by combat, by your tactics; when combat will not take them on, the approach fails as held up.
- buy {item, x?, y?}: walk onto a known shelf selling it (the nearest, or the one at x,y). Needs the gems; only shelves listed below can be named.
- equip {item}: arm or wear a carried item. Arming a potion does not drink it.
- drink {item?}: drink a carried potion now (the smallest when no item is named). Combat also drinks at the bar in a fight; this is for healing outside one.
- travel {x, y, map?, door?}: walk there, across maps by known doors; door true to go through the door at x,y.
- explore {x?, y?}: walk to unseen ground, toward x,y when given. Pair it with a found or seller goal.
- break_open {x, y, item, map?}: arm the item and use it on the block at x,y (a locked or hidden entrance, an odd block).
- forage: eat food in view, cut bushes for berries. recover: walk to her death chest and take everything back.
- find_boss: inside a level, take doors not taken and explore until the boss is in view. fight_boss: the boss fight on its clock.

fails_if (any of): minutes (how long it may run), no_progress_seconds (default: the patience lever), no_gem_minutes (earn_gems only). The method also fails on its own when it cannot go on (the field is held, no seller, nothing cuts grass); say what to do then in plan B.

tactics (what the reactions play while the approach runs; all optional): bars {<hostile type>: health} or bar (health) at which a fight stops being pushed: drink, else break off; avoid [<hostile type>]: never pick a fight with them; detour (blocks): how far an opportunity may lead her off; boss_seconds and leave_with_seconds for a boss fight.

- Only the goal, the approaches and next are carried out; nothing in why or notes is acted on. A step that must come first (arm the weapon just bought, eat before a fight) is the goal now, and what it was for goes in next.
- Plan A must be able to start now: a buy needs a seen shelf, a recover needs a death chest still out. Otherwise you are told why and asked again.
- Name places only by the keys and x, y listed below, items by their code.
- An approach that failed will fail the same way again unless something changed: choose differently.
"""


class Strategist:
    def __init__(self, client: LLMClient, cfg: ProviderConfig, record=None):
        self.client, self.cfg, self.record = client, cfg, record
        self.last: dict[str, Any] = {}  # its last answer: its chain and notes are read back to it
        self.budget, self.backoff = CallBudget(), Backoff()
        sections = [s.text for s in split_sections(read_text(REFERENCE_PATH)) if s.key.startswith(MANUAL)]
        self.system = (CONTRACT + "\nThe Manual (world, game rules, playing the world):\n" + "".join(sections)
                       + "\nGame notes (facts learned in play):\n" + game_notes_text())

    @classmethod
    def from_env(cls, record=None) -> Strategist:
        cfg = ProviderConfig.from_env()
        return cls(connect(cfg), cfg, record)

    def __call__(self, briefing: Briefing) -> dict[str, Any]:
        now = time.time()
        if self.backoff.waiting(now):
            raise RuntimeError("backing off after a failed call")
        messages = [{"role": "system", "content": self.system, "cache": True},
                    {"role": "user", "content": describe(briefing, self.last)}]
        self.budget.charge(now, messages)
        try:
            raw, usage = self.client.complete(messages)
        except Exception:
            self.backoff.fail(time.time())
            raise
        self.budget.settle(usage)
        self.backoff.ok()
        if self.record is not None:
            self.record({"t": now, "strategist": {"asked": briefing.reason, "messages": trace_messages(messages[1:]),
                                                  "reply": raw}})
        answer = parse_reply(raw, lambda o: isinstance(o, dict))
        if not isinstance(answer, dict):
            raise ValueError("the reply is not a JSON object")
        self.last = answer
        return answer


def describe(b: Briefing, last: dict[str, Any] | None = None) -> str:
    ctx: Ctx = b.ctx
    s, mem = ctx.state, ctx.memory
    lines = [f"Asked because: {b.reason}"]
    if b.note:
        lines.append(f"Your last answer had a problem: {b.note}")
    lines += ["", "## Lou now",
              f"health {s.health}/{s.max_health}, lives {s.lives}, gems {s.gems}",
              "carried: " + (", ".join(f"{c.code} ({c.where})" for c in s.carried) or "nothing"),
              f"levels cleared: {list(s.levels_cleared)} of {s.level_count or '?'}",
              f"on map {s.map_id} ({f'inside level {s.level}' if s.level else 'outside every level'}) at {s.pos[0]},{s.pos[1]}",
              "levers: " + ", ".join(f"{k} {v:g}" for k, v in vars(ctx.levers).items())]
    if ctx.town:
        lines.append(f"town (safe, bronze shop, respawn): map {ctx.town[0]} near {ctx.town[1][0]},{ctx.town[1][1]}")

    last = last or {}
    lines += ["", "## Running", f"{b.running}"]
    if last.get("chain"):
        lines += ["", "## Your chain last time (first unmet dependency last)"]
        lines += [f"- {c}" for c in last["chain"]]
    if last.get("notes"):
        lines += ["", f"## Your notes: {last['notes']}"]
    lines += ["", "## Outcomes (newest last)"]
    lines += [f"- {r}" for r in b.reports[-10:]] or ["none yet"]
    lines += ["", "## What just happened (the reactions, newest last)"]
    lines += [f"- {h}" for h in mem.happened] or ["nothing yet"]

    lines += ["", "## Entrances (the minimap marks every one; unnumbered until entered)"]
    for key, e in sorted(mem.entrances.items(), key=lambda kv: chebyshev((kv[1].x, kv[1].y), s.pos)):
        facts = [f"block {e.block}" if e.block else "not seen up close", "locked" if e.locked else "",
                 "reached" if e.reached else "", f"refused us: {e.refused}" if e.refused else "",
                 f"opens level {e.level}" if e.level else "", f"{chebyshev((e.x, e.y), s.pos)} blocks away" if e.map_id == s.map_id else ""]
        lines.append(f"- {key}: " + ", ".join(f for f in facts if f))
    if mem.levels:
        lines += ["", "## Levels"]
        for lv in sorted(mem.levels.values(), key=lambda lv: lv.number):
            tries = "; ".join(f"{a.result} after {a.seconds:.0f}s, boss health {a.boss_health_left}" for a in lv.attempts[-3:])
            lines.append(f"- level {lv.number}: entrance {lv.entrance or '?'}, maps {lv.maps}, boss {lv.boss or '?'}"
                         f" (max health {lv.boss_max_health or '?'}){'; attempts: ' + tries if tries else ''}")

    lines += ["", "## Every clue so far"]
    lines += [f"- {c.source} {c.speaker} at {c.map_id}:{c.x},{c.y}: {c.text}" for c in mem.clues] or ["none yet"]

    lines += ["", "## Seen for sale"]
    shops = sorted(mem.shops.items(), key=lambda kv: (kv[1].map_id != s.map_id, chebyshev((kv[1].x, kv[1].y), s.pos)))
    lines += [f"- {v.code} for {v.price} gems at {v.map_id}:{v.x},{v.y}" for _, v in shops[:SHOPS_SHOWN]] or ["nothing yet"]

    threats = s.known_threats()
    if threats or mem.territory:
        lines += ["", "## Hostiles known on this map"]
        lines += [f"- {e.code} {e.id} at {e.pos[0]},{e.pos[1]}" for e in threats[:15]]
        lines += [f"- {code} keeps to {', '.join(ground)}" for code, ground in mem.territory.items()]
    breaks = sum(y.breaks for y in mem.yields.values())
    gems = sum(y.gems for y in mem.yields.values())
    lines += ["", f"## Income: our breaks dropped {gems} gems in {breaks} breaks"]
    lines += [f"- {k.breaks} kills of {code} dropped {k.gems} gems" for code, k in mem.kills.items()]
    if mem.deaths:
        lines += ["", "## Deaths (newest last)"]
        lines += [f"- killed by {d.killer or '?'} at {d.map_id}:{d.x},{d.y}; chest {('still out at ' + f'{d.chest_map}:{d.chest_x},{d.chest_y}, holding ' + (', '.join(d.items) or 'what she carried (not recorded)')) if d.chest_id else 'gone or empty'}"
                  for d in mem.deaths[-5:]]

    lines += ["", "## How it has gone",
              f"minutes since the last milestone: {b.minutes_since_milestone:.1f}",
              "recent milestones: " + (", ".join(b.milestones) or "none")]
    return "\n".join(lines)
